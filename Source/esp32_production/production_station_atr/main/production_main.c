/* MCT431 ESP32 A: ATR over SLIP, CRC-16/CCITT, bounded stop-and-wait retries.
 * ATR (26 bytes): test_id:u16, seq:u32, payload_len:u16, payload[16], crc16:u16
 * Big-endian fields. Payload: kind:u8, value:u32, deterministic pad[11].
 * The part seq is the raw part ID. Gateway commands use unique seq identifiers.
 * Retries keep the original seq; ACK echoes it so repeated messages are safe.
 * IMPORTANT: UART0 is dedicated to binary SLIP/ATR data. No printf or UART0 logs.
 */
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"
#include "esp_timer.h"
#include "esp_err.h"

#define PORT UART_NUM_0
#define END 0xC0
#define ESC 0xDB
#define ESC_END 0xDC
#define ESC_ESC 0xDD
#define TEST_ID 0x4311
#define PAYLOAD_SIZE 16
#define ATR_SIZE (8 + PAYLOAD_SIZE + 2)
#define TYPE_PART 1
#define TYPE_ACK 2
#define TYPE_COMMAND 3
#define TYPE_COMMAND_ACK 4
#define TYPE_SET_CYCLE 5
#define TYPE_SET_CYCLE_ACK 6
#define TYPE_FAULT 7
#define CMD_START 1
#define CMD_PAUSE 2
#define CMD_RESUME 3
#define CMD_STOP 4
#define ACK_WAIT_MS 1200
#define MAX_PART_ATTEMPTS 3

static uint32_t cycle_ms = 2000;
typedef enum { STATE_IDLE, STATE_RUNNING, STATE_PAUSED } line_state_t;
static line_state_t line_state = STATE_IDLE;
static uint8_t rx_frame[ATR_SIZE + 8];
static size_t rx_len = 0;
static bool rx_escaped = false, rx_started = false;

/* Remember recent gateway commands, so a retransmission is ACKed without reapplying.
 * This is a bounded RAM cache; reset clears it. */
#define CMD_CACHE_SIZE 32
static uint32_t cmd_seq_cache[CMD_CACHE_SIZE];
static uint8_t cmd_kind_cache[CMD_CACHE_SIZE];
static uint32_t cmd_value_cache[CMD_CACHE_SIZE];
static unsigned cache_size = 0, cache_pos = 0;

static uint16_t crc16(const uint8_t *data, size_t n) {
    uint16_t crc = 0xFFFF;
    for (size_t i=0; i<n; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b=0; b<8; b++)
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1)^0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}
static void put16(uint8_t *p, uint16_t x) { p[0]=x>>8; p[1]=x; }
static void put32(uint8_t *p, uint32_t x) { p[0]=x>>24; p[1]=x>>16; p[2]=x>>8; p[3]=x; }
static uint16_t get16(const uint8_t *p) { return ((uint16_t)p[0]<<8)|p[1]; }
static uint32_t get32(const uint8_t *p) { return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3]; }

static void send_atr(uint8_t kind, uint32_t seq, uint32_t value) {
    uint8_t raw[ATR_SIZE] = {0};
    put16(raw, TEST_ID); put32(raw+2, seq); put16(raw+6, PAYLOAD_SIZE);
    raw[8] = kind; put32(raw+9, value);
    for (unsigned i=5; i<PAYLOAD_SIZE; i++) raw[8+i] = (uint8_t)(seq+i);
    put16(raw+ATR_SIZE-2, crc16(raw, ATR_SIZE-2));
    uint8_t slip[ATR_SIZE*2+2]; size_t used=0;
    slip[used++]=END;
    for (size_t i=0; i<ATR_SIZE; i++) {
        if (raw[i]==END) { slip[used++]=ESC; slip[used++]=ESC_END; }
        else if (raw[i]==ESC) { slip[used++]=ESC; slip[used++]=ESC_ESC; }
        else slip[used++]=raw[i];
    }
    slip[used++]=END;
    uart_write_bytes(PORT, (const char*)slip, used);
    uart_wait_tx_done(PORT, pdMS_TO_TICKS(100));
}

static bool seen_command(uint32_t seq, uint8_t kind, uint32_t value, bool *conflict) {
    for (unsigned i=0; i<cache_size; i++) if (cmd_seq_cache[i]==seq) {
        *conflict = cmd_kind_cache[i]!=kind || cmd_value_cache[i]!=value;
        return true;
    }
    *conflict=false;
    return false;
}
static void remember_command(uint32_t seq, uint8_t kind, uint32_t value) {
    cmd_seq_cache[cache_pos]=seq;
    cmd_kind_cache[cache_pos]=kind;
    cmd_value_cache[cache_pos]=value;
    cache_pos=(cache_pos+1)%CMD_CACHE_SIZE;
    if (cache_size<CMD_CACHE_SIZE) cache_size++;
}

static bool process_atr(const uint8_t *f, size_t len, uint32_t expected_part) {
    if (len!=ATR_SIZE || get16(f)!=TEST_ID || get16(f+6)!=PAYLOAD_SIZE) return false;
    if (crc16(f, ATR_SIZE-2)!=get16(f+ATR_SIZE-2)) return false;
    uint32_t seq=get32(f+2);
    for (unsigned i=5; i<PAYLOAD_SIZE; i++) if (f[8+i]!=(uint8_t)(seq+i)) return false;
    uint8_t kind=f[8]; uint32_t value=get32(f+9);
    if (kind==TYPE_ACK) return expected_part!=0 && seq==expected_part && value==expected_part;
    if (kind!=TYPE_COMMAND && kind!=TYPE_SET_CYCLE) return false;
    bool conflict=false;
    const bool duplicate=seen_command(seq,kind,value,&conflict);
    if (conflict) return false;
    if (!duplicate) {
        if (kind==TYPE_SET_CYCLE) {
            if (value<100 || value>10000) return false;
            cycle_ms=value;
        } else {
            switch(value) {
                case CMD_START: if(line_state==STATE_IDLE) line_state=STATE_RUNNING; break;
                case CMD_PAUSE: if(line_state==STATE_RUNNING) line_state=STATE_PAUSED; break;
                case CMD_RESUME: if(line_state==STATE_PAUSED) line_state=STATE_RUNNING; break;
                case CMD_STOP: line_state=STATE_IDLE; break;
                default: return false;
            }
        }
        remember_command(seq,kind,value);
    }
    send_atr(kind==TYPE_COMMAND ? TYPE_COMMAND_ACK : TYPE_SET_CYCLE_ACK,seq,value);
    return false;
}

static bool poll_serial(uint32_t expected_part, int duration_ms) {
    const int64_t deadline=esp_timer_get_time()+(int64_t)duration_ms*1000;
    while (esp_timer_get_time()<deadline) {
        uint8_t byte;
        if (uart_read_bytes(PORT,&byte,1,pdMS_TO_TICKS(10))!=1) continue;
        if (byte==END) {
            bool matched=false;
            if(rx_started && rx_len>0 && !rx_escaped)
                matched=process_atr(rx_frame,rx_len,expected_part);
            rx_started=true; rx_escaped=false; rx_len=0;
            if(matched) return true;
            continue;
        }
        if(!rx_started) continue;
        if(rx_escaped) {
            if(byte==ESC_END) byte=END;
            else if(byte==ESC_ESC) byte=ESC;
            else {rx_started=false;rx_len=0;rx_escaped=false;continue;}
            rx_escaped=false;
        } else if(byte==ESC) {rx_escaped=true;continue;}
        if(rx_len<sizeof(rx_frame)) rx_frame[rx_len++]=byte;
        else {rx_started=false;rx_len=0;rx_escaped=false;}
    }
    return false;
}

void app_main(void) {
    const uart_config_t cfg={.baud_rate=115200,.data_bits=UART_DATA_8_BITS,
       .parity=UART_PARITY_DISABLE,.stop_bits=UART_STOP_BITS_1,
       .flow_ctrl=UART_HW_FLOWCTRL_DISABLE,.source_clk=UART_SCLK_DEFAULT};
    ESP_ERROR_CHECK(uart_param_config(PORT,&cfg));
    ESP_ERROR_CHECK(uart_driver_install(PORT,2048,2048,0,NULL,0));
    uint32_t part=1;
    while(true) {
        if(line_state!=STATE_RUNNING) {poll_serial(0,100);continue;}
        bool acked=false;
        for(int attempt=1; attempt<=MAX_PART_ATTEMPTS && line_state==STATE_RUNNING; attempt++) {
            send_atr(TYPE_PART,part,part);
            acked=poll_serial(part,ACK_WAIT_MS);
            if(acked) break;
        }
        if(line_state!=STATE_RUNNING) continue;
        if(!acked) {
            line_state=STATE_IDLE;
            send_atr(TYPE_FAULT,part,part);
            continue;
        }
        part++;
        for(uint32_t ms=0; ms<cycle_ms && line_state==STATE_RUNNING; ms+=50) poll_serial(0,50);
    }
}
