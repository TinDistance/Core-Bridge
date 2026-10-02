#include "ti_msp_dl_config.h"
#include "OLED.h"
#include <string.h>
#include <stdbool.h>
#include <stdint.h>

/* ---- UART3 RX ring buffer ---- */
#define RX_BUF_SIZE   128
static volatile char    rx_buf[RX_BUF_SIZE];
static volatile uint8_t rx_head = 0, rx_tail = 0;

/* ---- OLED mini terminal: 128x64, 6x8 font -> 21 cols x 8 rows ---- */
#define TERM_COLS 21
#define TERM_ROWS 8
static char term[TERM_ROWS][TERM_COLS + 1];
static uint8_t term_row = 0, term_col = 0;
static volatile bool term_dirty = false;

static void uart3_send_char(char ch)
{
    DL_UART_transmitDataBlocking(UART_3_INST, (uint8_t)ch);
}

static void term_putc(char ch)
{
    if (ch == '\r')
        return;
    if (ch == '\n') {
        term_row = (term_row + 1) % TERM_ROWS;
        term_col = 0;
        memset(term[term_row], 0, TERM_COLS + 1);
        term_dirty = true;
        return;
    }
    if (ch < 32 || ch > 126)
        ch = '.';
    term[term_row][term_col] = ch;
    if (++term_col >= TERM_COLS) {
        term_col = 0;
        term_row = (term_row + 1) % TERM_ROWS;
        memset(term[term_row], 0, TERM_COLS + 1);
    }
    term_dirty = true;
}

static void term_refresh(void)
{
    OLED_Clear();
    for (int i = 0; i < TERM_ROWS; i++)
        OLED_ShowString(0, i * 8, term[i], OLED_6X8_HALF);
    OLED_Update();
}

void UART_3_INST_IRQHandler(void)
{
    switch (DL_UART_getPendingInterrupt(UART_3_INST)) {
    case DL_UART_IIDX_RX: {
        char ch = (char)DL_UART_Main_receiveData(UART_3_INST);
        uint8_t next = (uint8_t)((rx_head + 1) % RX_BUF_SIZE);
        if (next != rx_tail) {
            rx_buf[rx_head] = ch;
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

    OLED_Init();
    NVIC_ClearPendingIRQ(UART_3_INST_INT_IRQN);
    NVIC_EnableIRQ(UART_3_INST_INT_IRQN);

    OLED_Clear();
    OLED_ShowString(0, 0,  "UART3 -> OLED", OLED_8X16_HALF);
    OLED_ShowString(0, 24, "PA14 TX / PA25 RX", OLED_6X8_HALF);
    OLED_ShowString(0, 34, "115200 8N1", OLED_6X8_HALF);
    OLED_ShowString(0, 52, "waiting data...", OLED_6X8_HALF);
    OLED_Update();

    uart3_send_char('R');  /* ready */
    uart3_send_char('D');
    uart3_send_char('Y');
    uart3_send_char('\r');
    uart3_send_char('\n');

    while (1) {
        while (rx_tail != rx_head) {
            char ch = rx_buf[rx_tail];
            rx_tail = (uint8_t)((rx_tail + 1) % RX_BUF_SIZE);
            uart3_send_char(ch);   /* echo back */
            term_putc(ch);
        }
        if (term_dirty) {
            term_dirty = false;
            term_refresh();
        }
    }
}
