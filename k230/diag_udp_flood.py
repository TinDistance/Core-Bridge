# 验证实验 E1：UDP goodput 泛洪发送端（K230 上运行）。
#
# 配合 test/udp_flood_rx.py：PC 上先起接收端
#     python test/udp_flood_rx.py --port 9001 --seconds 30
# 然后在 K230 跑本脚本，观察接收端打印的 pps/Mbps 与丢包。
# 目标：>= 8Mbps 稳定、丢包 < 0.2%（方案 A 的 3Mbps 预算前提）。
import socket
import struct
import time

import network

# ==================== 配置区 ====================
WIFI_SSID = "TF-Laptop"
WIFI_PASSWORD = "TF@HTR.Hello"
SERVER_IP = None        # None = 用网关地址
FLOOD_PORT = 9001
PAYLOAD = 1200          # 与 RTP 实际包大小一致
DURATION_S = 30
RATE_PPS = 500          # 目标速率（约 4.8Mbps）；可调大找 goodput 上限
# ================================================


def main():
    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)
    if not sta.isconnected():
        print("connecting to " + WIFI_SSID)
        sta.connect(WIFI_SSID, WIFI_PASSWORD)
        for _ in range(15):
            if sta.isconnected():
                break
            time.sleep(1)
    ip, _nm, gw, _dns = sta.ifconfig()
    print("IP: %s GW: %s" % (ip, gw))
    server_ip = SERVER_IP or gw

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = struct.pack(">H", 0xF00D) * (PAYLOAD // 2)
    addr = (server_ip, FLOOD_PORT)
    print("flooding %s:%d @%dpps for %ds ..." % (
        server_ip, FLOOD_PORT, RATE_PPS, DURATION_S))

    interval_us = 1000000 // RATE_PPS
    sent = 0
    errors = 0
    t0 = time.ticks_us()
    next_send = t0
    t_end = time.ticks_add(t0, DURATION_S * 1000000)
    while time.ticks_diff(t_end, time.ticks_us()) > 0:
        now = time.ticks_us()
        ahead = time.ticks_diff(next_send, now)
        if ahead > 0:
            time.sleep_us(max(0, ahead - 200))
        try:
            sock.sendto(payload, addr)
            sent += 1
        except Exception as e:
            errors += 1
            if errors % 100 == 1:
                print("send err: " + str(e))
        next_send = time.ticks_add(next_send, interval_us)
        if next_send < now:
            next_send = time.ticks_add(now, interval_us)
    dt = time.ticks_diff(time.ticks_us(), t0) / 1000000.0
    print("done: sent=%d err=%d avg=%.0f pps (~%.2f Mbps)" % (
        sent, errors, sent / dt, sent * PAYLOAD * 8 / dt / 1e6))


main()
