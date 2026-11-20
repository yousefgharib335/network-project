/* MCT431 ESP32 B - length-prefixed TCP, runtime N/K inspection.
 * TCP frames: 2-byte big-endian message length + ASCII application payload (no newline).
 * CONFIG <cmd_id> <N> <K> <inspection_ms>
 * CONFIG_ACK <cmd_id> APPLIED <N> <K> <inspection_ms>
 * INSPECT <job_id> <original_part_id> -> ACCEPT <job_id> <original_part_id>
 * -> RESULT <job_id> <original_part_id> PASS|FAIL
 * job_id is globally unique for deduplication; verdict uses original_part_id.
 */
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <inttypes.h>
#include <ctype.h>
#include <limits.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "lwip/inet.h"

#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define GATEWAY_IP "192.168.1.113"
#define GATEWAY_PORT 9000

static const char *TAG = "MCT431_INSPECTION";
static EventGroupHandle_t wifi_events;
#define WIFI_CONNECTED_BIT BIT0

/* Defaults preserve old parity test when used by the existing gateway.
 * CONFIG changes these LIVE without reflashing.
 */
static uint32_t rule_n = 2;
static uint32_t rule_k = 1;
static uint32_t inspection_ms = 0;
static bool config_seen = false;
static uint32_t last_cmd_id = 0, last_n = 0, last_k = 0, last_ms = 0;

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    (void)arg;
    if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "Wi-Fi disconnected; reconnecting");
        esp_wifi_connect();
    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *evt = (const ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Wi-Fi IP: " IPSTR, IP2STR(&evt->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_start(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               wifi_event_handler, NULL));
    wifi_config_t wifi_cfg = {0};
    strncpy((char *)wifi_cfg.sta.ssid, WIFI_SSID, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, WIFI_PASSWORD, sizeof(wifi_cfg.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
}

static bool send_all(int sock, const char *buffer, size_t length)
{
    size_t sent = 0;
    while (sent < length) {
        int n = send(sock, buffer + sent, length - sent, 0);
        if (n <= 0) return false;
        sent += (size_t)n;
    }
    return true;
}

/* One TCP frame: uint16 big-endian length followed by ASCII bytes (no LF). */
#define MAX_APP_MESSAGE 160
#define JOB_CACHE_SIZE 256
static uint32_t job_ids[JOB_CACHE_SIZE];
static uint32_t job_part_ids[JOB_CACHE_SIZE];
static bool job_failed[JOB_CACHE_SIZE];
static size_t job_cache_used = 0;
static size_t job_cache_next = 0;

static bool send_text(int sock, const char *text)
{
    size_t len = strlen(text);
    if (!len || len > MAX_APP_MESSAGE) return false;
    if (text[len - 1] == '\n') --len;  /* reuse existing reply strings */
    if (!len) return false;
    unsigned char header[2] = {(unsigned char)(len >> 8), (unsigned char)(len & 0xFF)};
    return send_all(sock, (const char *)header, 2) && send_all(sock, text, len);
}

static bool read_exact(int sock, void *destination, size_t size)
{
    unsigned char *buffer = destination;
    size_t got = 0;
    while (got < size) {
        int n = recv(sock, buffer + got, size - got, 0);
        if (n <= 0) return false;
        got += (size_t)n;
    }
    return true;
}

static bool read_frame(int sock, char message[MAX_APP_MESSAGE + 1])
{
    unsigned char header[2];
    if (!read_exact(sock, header, 2)) return false;
    size_t len = ((size_t)header[0] << 8) | header[1];
    if (len == 0 || len > MAX_APP_MESSAGE) return false;
    if (!read_exact(sock, message, len)) return false;
    message[len] = '\0';
    if (memchr(message, '\0', len) != NULL) return false;
    return true;
}

static bool cached_verdict(uint32_t job_id, uint32_t part_id, bool *failed, bool *conflict)
{
    for (size_t i = 0; i < job_cache_used; ++i) {
        if (job_ids[i] == job_id) {
            *conflict = job_part_ids[i] != part_id;
            *failed = job_failed[i];
            return true;
        }
    }
    return false;
}

static void remember_verdict(uint32_t job_id, uint32_t part_id, bool failed)
{
    job_ids[job_cache_next] = job_id;
    job_part_ids[job_cache_next] = part_id;
    job_failed[job_cache_next] = failed;
    job_cache_next = (job_cache_next + 1) % JOB_CACHE_SIZE;
    if (job_cache_used < JOB_CACHE_SIZE) ++job_cache_used;
}

static bool parse_u32(const char *text, uint32_t *value)
{
    if (!text || !*text) return false;
    for (const char *p = text; *p; ++p) {
        if (!isdigit((unsigned char)*p)) return false;
    }
    errno = 0;
    char *end;
    unsigned long v = strtoul(text, &end, 10);
    if (errno || *end || v > UINT32_MAX) return false;
    *value = (uint32_t)v;
    return true;
}

static bool respond_to_line(int sock, const char *input)
{
    char line[160];
    char reply[160];
    size_t size = strlen(input);
    if (size >= sizeof(line)) return send_text(sock, "ERROR OVERLONG\n");
    memcpy(line, input, size + 1);

    char *save = NULL;
    char *op = strtok_r(line, " ", &save);
    if (!op) return send_text(sock, "ERROR EMPTY\n");

    if (strcmp(op, "PING") == 0) {
        char *token = strtok_r(NULL, " ", &save);
        uint32_t id;
        if (!parse_u32(token, &id) || strtok_r(NULL, " ", &save))
            return send_text(sock, "ERROR BAD_PING\n");
        int n = snprintf(reply, sizeof(reply), "PONG %" PRIu32 "\n", id);
        return n > 0 && (size_t)n < sizeof(reply) && send_text(sock, reply);
    }

    if (strcmp(op, "CONFIG") == 0) {
        char *a = strtok_r(NULL, " ", &save);
        char *b = strtok_r(NULL, " ", &save);
        char *c = strtok_r(NULL, " ", &save);
        char *d = strtok_r(NULL, " ", &save);
        uint32_t cmd_id, nval, kval, msval;
        if (!parse_u32(a, &cmd_id) || !parse_u32(b, &nval) ||
            !parse_u32(c, &kval) || !parse_u32(d, &msval) ||
            strtok_r(NULL, " ", &save) || cmd_id == 0 ||
            nval == 0 || nval > 1000000 || kval >= nval || msval > 10000) {
            return send_text(sock, "CONFIG_NACK INVALID\n");
        }
        if (config_seen && cmd_id == last_cmd_id) {
            if (nval != last_n || kval != last_k || msval != last_ms)
                return send_text(sock, "CONFIG_NACK ID_REUSE\n");
            ESP_LOGI(TAG, "Duplicate CONFIG id=%" PRIu32 " (not applied twice)", cmd_id);
        } else {
            if (config_seen && cmd_id < last_cmd_id)
                return send_text(sock, "CONFIG_NACK STALE_ID\n");
            rule_n = nval;
            rule_k = kval;
            inspection_ms = msval;
            last_cmd_id = cmd_id;
            last_n = nval;
            last_k = kval;
            last_ms = msval;
            config_seen = true;
            ESP_LOGI(TAG, "CONFIG applied id=%" PRIu32 " N=%" PRIu32 " K=%" PRIu32 " delay=%" PRIu32 "ms", cmd_id, rule_n, rule_k, inspection_ms);
        }
        int n = snprintf(reply, sizeof(reply),
                         "CONFIG_ACK %" PRIu32 " APPLIED %" PRIu32 " %" PRIu32 " %" PRIu32 "\n",
                         cmd_id, rule_n, rule_k, inspection_ms);
        return n > 0 && (size_t)n < sizeof(reply) && send_text(sock, reply);
    }

    if (strcmp(op, "INSPECT") == 0) {
        char *job_token = strtok_r(NULL, " ", &save);
        char *part_token = strtok_r(NULL, " ", &save);
        uint32_t job_id, original_part_id;
        if (!parse_u32(job_token, &job_id) || !parse_u32(part_token, &original_part_id) ||
            job_id == 0 || original_part_id == 0 || strtok_r(NULL, " ", &save))
            return send_text(sock, "ERROR BAD_ID\n");
        bool fail = false, conflict = false;
        bool duplicate = cached_verdict(job_id, original_part_id, &fail, &conflict);
        if (conflict) return send_text(sock, "ERROR ID_REUSE\n");
        int n = snprintf(reply, sizeof(reply), "ACCEPT %" PRIu32 " %" PRIu32,
                         job_id, original_part_id);
        if (n <= 0 || (size_t)n >= sizeof(reply) || !send_text(sock, reply)) return false;
        if (duplicate) {
            ESP_LOGI(TAG, "Duplicate inspection job=%" PRIu32 " part=%" PRIu32 " - cached verdict",
                     job_id, original_part_id);
        } else {
            /* The configured inspection rule must apply to ESP32 A's unchanged part ID. */
            fail = (((uint64_t)original_part_id + (uint64_t)rule_k) % rule_n) == 0;
            if (inspection_ms) vTaskDelay(pdMS_TO_TICKS(inspection_ms));
            remember_verdict(job_id, original_part_id, fail);
        }
        const char *verdict = fail ? "FAIL" : "PASS";
        n = snprintf(reply, sizeof(reply), "RESULT %" PRIu32 " %" PRIu32 " %s",
                     job_id, original_part_id, verdict);
        ESP_LOGI(TAG, "INSPECT job=%" PRIu32 " original_part=%" PRIu32 " -> %s",
                 job_id, original_part_id, verdict);
        return n > 0 && (size_t)n < sizeof(reply) && send_text(sock, reply);
    }

    return send_text(sock, "ERROR UNKNOWN\n");
}

void app_main(void)
{
    ESP_LOGI(TAG, "Runtime-configurable inspection station starting");
    wifi_start();
    while (1) {
        xEventGroupWaitBits(wifi_events, WIFI_CONNECTED_BIT, pdFALSE,
                            pdFALSE, portMAX_DELAY);
        int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        struct sockaddr_in addr = {0};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(GATEWAY_PORT);
        if (inet_pton(AF_INET, GATEWAY_IP, &addr.sin_addr) != 1) {
            ESP_LOGE(TAG, "Invalid GATEWAY_IP");
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        ESP_LOGI(TAG, "Connecting to %s:%d", GATEWAY_IP, GATEWAY_PORT);
        if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
            ESP_LOGW(TAG, "TCP connect failed, errno=%d", errno);
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        if (!send_text(sock, "HELLO ESP32_B")) {
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        ESP_LOGI(TAG, "TCP connected (2-byte length prefix)");
        char message[MAX_APP_MESSAGE + 1];
        while (xEventGroupGetBits(wifi_events) & WIFI_CONNECTED_BIT) {
            if (!read_frame(sock, message)) break;
            if (!respond_to_line(sock, message)) break;
        }
        ESP_LOGW(TAG, "TCP connection closed; retrying");
        shutdown(sock, SHUT_RDWR);
        close(sock);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
