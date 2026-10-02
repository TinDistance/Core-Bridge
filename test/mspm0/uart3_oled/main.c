#include "ti_msp_dl_config.h"
#include "OLED.h"
#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <stdint.h>

/* ================= UART3 RX 环形缓冲（115200 8N1，PA14 TX / PA25 RX） ================= */
#define RX_BUF_SIZE 256u
#define RX_BUF_MASK (RX_BUF_SIZE - 1u)
static volatile uint8_t  rx_buf[RX_BUF_SIZE];
static volatile uint16_t rx_head = 0;
static volatile uint16_t rx_tail = 0;

static bool rx_pop(uint8_t *out)
{
    uint16_t head = rx_head; /* 单次 volatile 读；M0+ 上对齐半字读是原子的 */
    if (rx_tail == head)
        return false;
    *out = rx_buf[rx_tail];
    rx_tail = (uint16_t)((rx_tail + 1u) & RX_BUF_MASK);
    return true;
}

/* ================= 毫秒时间戳：SysTick 1kHz ================= */
static volatile uint32_t g_ms = 0;

void SysTick_Handler(void)
{
    g_ms++;
}

/* ================= v2 命令帧解析器 =================
 * [0]=0xAA [1]=0x55 [2]=N(1..8) { [id][len][payload] }xN [末]=crc8(XOR)
 * 0x01 MOVE len=2: speed i8, turn i8 / 0x02 TURRET len=2: yaw i8, pitch i8
 */
#define CMD_MOVE            0x01u
#define CMD_TURRET          0x02u
#define FRAME_N_MIN         1u
#define FRAME_N_MAX         8u
#define ENTRY_LEN_MAX       8u
#define ENTRIES_BYTES_MAX   32u   /* sum over entries of (2 + len) */
#define FRAME_GAP_MS        20u   /* 部分帧超时丢弃 */
#define CMD_TIMEOUT_MS      300u  /* 无合法帧超时显示 NO COMMAND */
#define REDRAW_MIN_MS       30u   /* 两次 OLED 重绘最小间隔 */

typedef struct {
    bool   has_move;
    int8_t move_spd;
    int8_t move_trn;
    bool   has_turret;
    int8_t tur_yaw;
    int8_t tur_ptc;
} cmd_snapshot_t;

typedef enum {
    PS_WAIT_AA = 0,
    PS_WAIT_55,
    PS_READ_N,
    PS_READ_ID,
    PS_READ_LEN,
    PS_READ_PAYLOAD,
    PS_READ_CRC
} parser_state_t;

static parser_state_t ps = PS_WAIT_AA;
static uint8_t  ps_xor;
static uint8_t  ps_n;
static uint8_t  ps_idx;              /* 已收齐条目数 */
static uint16_t ps_entries_bytes;    /* 已收条目累计 (2 + len) */
static uint8_t  ps_id;
static uint8_t  ps_len;
static uint8_t  ps_pay_idx;
static uint8_t  ps_payload[ENTRY_LEN_MAX];
static uint32_t ps_last_ms;          /* 状态机最近一次推进时刻 */
static cmd_snapshot_t staging;       /* 正在组装的帧 */

static cmd_snapshot_t latest;
static bool     latest_valid = false;
static uint32_t latest_ms = 0;
static bool     frame_ready = false; /* 主循环待重绘标志 */

static void parser_reset(void)
{
    ps = PS_WAIT_AA;
}

static void staging_commit_entry(void)
{
    if (ps_id == CMD_MOVE && ps_len == 2u) {
        staging.has_move = true;
        staging.move_spd = (int8_t)ps_payload[0];
        staging.move_trn = (int8_t)ps_payload[1];
    } else if (ps_id == CMD_TURRET && ps_len == 2u) {
        staging.has_turret = true;
        staging.tur_yaw = (int8_t)ps_payload[0];
        staging.tur_ptc = (int8_t)ps_payload[1];
    }
    /* 未知 id、或已知 id 但 len 不符：按 len 跳过，不影响其他条目 */
}

static void parser_advance_entry(uint32_t now)
{
    ps_idx++;
    ps_last_ms = now;
    ps = (ps_idx >= ps_n) ? PS_READ_CRC : PS_READ_ID;
}

static void parser_feed(uint8_t b, uint32_t now)
{
    switch (ps) {
    case PS_WAIT_AA:
        if (b == 0xAAu) {
            ps = PS_WAIT_55;
            ps_last_ms = now;
        }
        break;
    case PS_WAIT_55:
        if (b == 0x55u) {
            ps = PS_READ_N;
            ps_xor = (uint8_t)(0xAAu ^ 0x55u);
            ps_last_ms = now;
        } else if (b == 0xAAu) {
            ps = PS_WAIT_55; /* 重叠帧头：AA AA 55 的第二个 AA */
            ps_last_ms = now;
        } else {
            ps = PS_WAIT_AA;
        }
        break;
    case PS_READ_N:
        if (b >= FRAME_N_MIN && b <= FRAME_N_MAX) {
            ps_n = b;
            ps_idx = 0;
            ps_entries_bytes = 0;
            memset(&staging, 0, sizeof(staging));
            ps_xor ^= b;
            ps = PS_READ_ID;
            ps_last_ms = now;
        } else if (b == 0xAAu) {
            ps = PS_WAIT_55;
            ps_last_ms = now;
        } else {
            ps = PS_WAIT_AA;
        }
        break;
    case PS_READ_ID:
        /* 任何 id 都接受（含未知 id，后续按 len 跳过；不做 AA 重同步，
           否则会破坏未知条目的跳过语义） */
        ps_id = b;
        ps_xor ^= b;
        ps = PS_READ_LEN;
        ps_last_ms = now;
        break;
    case PS_READ_LEN:
        if (b > ENTRY_LEN_MAX ||
            (ps_entries_bytes + 2u + (uint16_t)b) > ENTRIES_BYTES_MAX) {
            if (b == 0xAAu) {
                ps = PS_WAIT_55;
                ps_last_ms = now;
            } else {
                ps = PS_WAIT_AA;
            }
        } else {
            ps_len = b;
            ps_xor ^= b;
            ps_entries_bytes += (uint16_t)(2u + b);
            if (b == 0u) {
                staging_commit_entry(); /* len=0：空条目，直接记完成 */
                parser_advance_entry(now);
            } else {
                ps_pay_idx = 0;
                ps = PS_READ_PAYLOAD;
                ps_last_ms = now;
            }
        }
        break;
    case PS_READ_PAYLOAD:
        ps_payload[ps_pay_idx++] = b;
        ps_xor ^= b;
        ps_last_ms = now;
        if (ps_pay_idx >= ps_len) {
            staging_commit_entry();
            parser_advance_entry(now);
        }
        break;
    case PS_READ_CRC:
        if (b == ps_xor) {
            latest = staging;
            latest_valid = true;
            latest_ms = now;
            frame_ready = true;
        }
        if (b == 0xAAu) {
            ps = PS_WAIT_55;
            ps_last_ms = now;
        } else {
            ps = PS_WAIT_AA;
        }
        break;
    default:
        ps = PS_WAIT_AA;
        break;
    }
}

/* ================= OLED 显示（128x64，6x8 字体=21列，标题用 8x16） ================= */
static cmd_snapshot_t shown;
static bool     shown_valid = false; /* false = 当前屏上是 NO COMMAND */
static bool     draw_pending = false;
static uint32_t last_draw_ms = 0;

static bool snapshot_equal(const cmd_snapshot_t *a, const cmd_snapshot_t *b)
{
    return (a->has_move == b->has_move) &&
           (a->move_spd == b->move_spd) &&
           (a->move_trn == b->move_trn) &&
           (a->has_turret == b->has_turret) &&
           (a->tur_yaw == b->tur_yaw) &&
           (a->tur_ptc == b->tur_ptc);
}

static void draw_commands(const cmd_snapshot_t *s)
{
    /* 最坏行 "TURT YAW-128 PTC-128" = 20 字符 x 6px = 120 <= 128，恒不溢出 */
    char line[24];
    uint8_t y = 20u;

    OLED_Clear();
    OLED_ShowString(0, 0, (char *)"CMD", OLED_8X16_HALF);
    if (s->has_move) {
        snprintf(line, sizeof(line), "MOVE SPD%+d TRN%+d",
                 (int)s->move_spd, (int)s->move_trn);
        OLED_ShowString(0, y, line, OLED_6X8_HALF);
        y += 12u;
    }
    if (s->has_turret) {
        snprintf(line, sizeof(line), "TURT YAW%+d PTC%+d",
                 (int)s->tur_yaw, (int)s->tur_ptc);
        OLED_ShowString(0, y, line, OLED_6X8_HALF);
        y += 12u;
    }
    OLED_Update();
}

static void draw_no_command(void)
{
    /* "NO COMMAND" 10字符 x 8px = 80，x=(128-80)/2=24 居中 */
    OLED_Clear();
    OLED_ShowString(24, 24, (char *)"NO COMMAND", OLED_8X16_HALF);
    OLED_Update();
}

/* ================= UART3 中断：只收不回（原 echo 已删） ================= */
void UART_3_INST_IRQHandler(void)
{
    switch (DL_UART_getPendingInterrupt(UART_3_INST)) {
    case DL_UART_IIDX_RX: {
        uint8_t b = DL_UART_Main_receiveData(UART_3_INST);
        uint16_t next = (uint16_t)((rx_head + 1u) & RX_BUF_MASK);
        if (next != rx_tail) {
            rx_buf[rx_head] = b;
            rx_head = next;
        }
        break;
    }
    default:
        break;
    }
}

int main(void)
{
    SYSCFG_DL_init();

    /* 1kHz SysTick：reload = CPUCLK_FREQ/1000 = 80000（< 2^24，合法） */
    if (SysTick_Config(CPUCLK_FREQ / 1000U) != 0u) {
        while (1) { }
    }

    OLED_Init();
    NVIC_ClearPendingIRQ(UART_3_INST_INT_IRQN);
    NVIC_EnableIRQ(UART_3_INST_INT_IRQN);

    draw_no_command(); /* 上电从未收帧：直接显示 NO COMMAND */
    last_draw_ms = g_ms;

    while (1) {
        uint32_t now = g_ms;
        uint8_t b;

        while (rx_pop(&b)) {
            parser_feed(b, now);
            now = g_ms;
        }
        now = g_ms;

        /* 部分帧 20ms 没收齐则丢弃（含缓冲已空、等不到后续字节的情况） */
        if (ps != PS_WAIT_AA && (now - ps_last_ms) > FRAME_GAP_MS)
            parser_reset();

        now = g_ms;
        bool want_cmd = latest_valid && ((now - latest_ms) <= CMD_TIMEOUT_MS);

        if (frame_ready) {
            frame_ready = false;
            if (want_cmd && (!shown_valid || !snapshot_equal(&shown, &latest)))
                draw_pending = true; /* 内容变化才重绘 */
        }
        if (!want_cmd)
            draw_pending = false; /* 已过期：挂起的重绘作废，转 NO COMMAND */

        /* 合法新帧：距上次重绘满 30ms 则当次循环立即重绘 */
        if (want_cmd && draw_pending && (now - last_draw_ms) >= REDRAW_MIN_MS) {
            draw_commands(&latest);
            shown = latest;
            shown_valid = true;
            last_draw_ms = now;
            draw_pending = false;
        }

        /* 超时切 NO COMMAND：主循环无阻塞，超时后 1 个循环内进入本分支，
           节流上限 30ms < 50ms 预算 */
        if (!want_cmd && shown_valid) {
            if ((now - last_draw_ms) >= REDRAW_MIN_MS) {
                draw_no_command();
                shown_valid = false;
                last_draw_ms = now;
            }
        }
    }
}
