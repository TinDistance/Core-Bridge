import gc
import os
import socket
import struct
import time
import uctypes

import network
from media.sensor import *
from media.media import *
from media.vencoder import *

SERVER_IP = "172.20.10.6"
RTP_PORT = 8002

TIERS = {
    "T1": (1280, 720, 3000),
    "T2": (1280, 720, 4500),
    "T3": (1920, 1080, 6000),
}
SELECTED_TIER = "T3"
FRAME_WIDTH, FRAME_HEIGHT, ENCODER_BITRATE_KBPS = TIERS[SELECTED_TIER]

WIFI_LINK_KBPS = 200000
SENSOR_ID = 2
FPS = 30
GOP_LENGTH = 60
MAX_NALU_CHUNK_SIZE = 1400
UART_BAUDRATE = 115200
MOTOR_SPEED_HZ = 20000  # PUL脉冲频率; 对齐STM32F103例程(main.c): 6400边沿=3200脉冲/圈(16细分)=一圈; RPM = FREQ*60/3200
MOTOR_DIRECTION = 0  # DIR电平(例程PA7, 翻转换向、起始为低): 0=低电平=CW正转, 1=高电平=CCW(与例程翻转方向对应的出厂默认含义)
GC_COLLECT_EVERY = 100
WIFI_WAIT_LOG_MS = 5000

CONTROL_MAGIC = b"CBR"
CTRL_IDR = 0x01
CTRL_NACK = 0x03
MAX_NACK_SEQUENCES = 32
RETRANSMIT_BUDGET_RATIO = 0.15
RETRANSMIT_WINDOW_MS = 1000
IDR_REQUEST_INTERVAL_MS = 400
IDR_SETTLE_MS = 500
COMMAND_STALE_MS = 200
UART_TX_PERIOD_MS = 20
UART_TX_IDLE_FRAME = bytes((
    0xAA, 0x55, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0xD1, 0x1E, 0x0D, 0x0A,
))

RTP_CLOCK_HZ = 90000
RTP_TICKS_PER_MS = RTP_CLOCK_HZ // 1000
RTP_HEADER_BYTES = 12
UDP_IP_OVERHEAD_BYTES = 28
WIRE_OVERHEAD_BYTES = RTP_HEADER_BYTES + UDP_IP_OVERHEAD_BYTES
TICKS_MS_PERIOD = 1 << 30
SEND_RETRY_LIMIT = 3
SEND_RETRY_DELAY_US = 500

sensor = None
encoder = None
media_link = None
sta = None
uart = None
motor_pwm = None
motor_dir_pin = None
motor_en_pin = None
motor_running = False


def ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def ticks_us():
    return time.ticks_us() if hasattr(time, "ticks_us") else int(time.time() * 1000000)


def ticks_diff(start, end):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(start, end)
    difference = start - end
    return difference + TICKS_MS_PERIOD if difference < 0 else difference


def sleep_us(duration):
    if hasattr(time, "sleep_us"):
        time.sleep_us(duration)
    else:
        time.sleep(duration / 1000000.0)


def wait_for_wifi(wlan, log_interval_ms=WIFI_WAIT_LOG_MS):
    if wlan.isconnected():
        ip, _, gateway, _ = wlan.ifconfig()
        print("[wifi] connected: IP=%s GW=%s" % (ip, gateway))
        return

    print("[wifi] waiting for connection; reconnect is not triggered here")
    start_ms = ticks_ms()
    last_log_ms = start_ms
    while not wlan.isconnected():
        os.exitpoint()
        time.sleep(1)
        now_ms = ticks_ms()
        if ticks_diff(now_ms, last_log_ms) >= log_interval_ms:
            print("[wifi] still waiting: %ds" % (ticks_diff(now_ms, start_ms) // 1000))
            last_log_ms = now_ms

    ip, _, gateway, _ = wlan.ifconfig()
    print("[wifi] connected: IP=%s GW=%s (waited %ds)" % (
        ip, gateway, ticks_diff(ticks_ms(), start_ms) // 1000))


def init_wifi():
    global sta
    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)
    wait_for_wifi(sta)
    return sta


def init_camera():
    global sensor
    sensor = Sensor(id=SENSOR_ID)
    sensor.reset()
    aligned_width = ALIGN_UP(FRAME_WIDTH, 16)
    sensor.set_framesize(
        width=aligned_width, height=FRAME_HEIGHT,
        alignment=12, chn=CAM_CHN_ID_0
    )
    sensor.set_pixformat(Sensor.YUV420SP, chn=CAM_CHN_ID_0)
    print("[1/4] camera ready: %dx%d (%s)" % (
        aligned_width, FRAME_HEIGHT, SELECTED_TIER))


def select_h264_profile(video_encoder):
    for profile_name in ("H264_PROFILE_BASELINE", "H264_PROFILE_MAIN"):
        profile = getattr(video_encoder, profile_name, None)
        if profile is not None:
            return profile, profile_name
    raise RuntimeError("No H.264 profile constant found")


def init_encoder():
    global encoder, media_link
    aligned_width = ALIGN_UP(FRAME_WIDTH, 16)
    encoder = Encoder()
    encoder.SetOutBufs(8, aligned_width, FRAME_HEIGHT)
    profile, profile_name = select_h264_profile(encoder)
    channel_attributes = ChnAttrStr(
        encoder.PAYLOAD_TYPE_H264,
        profile,
        aligned_width,
        FRAME_HEIGHT,
        bit_rate=ENCODER_BITRATE_KBPS,
        gopLen=GOP_LENGTH,
        src_frame_rate=FPS,
        dst_frame_rate=FPS,
    )
    encoder.Create(channel_attributes)
    media_link = MediaManager.link(
        sensor.bind_info()["src"],
        (VIDEO_ENCODE_MOD_ID, VENC_DEV_ID, encoder.chn),
    )
    encoder.Start()
    sensor.run()
    print("[3/4] encoder ready: ch=%d %dx%d %s %dkbps GOP=%d" % (
        encoder.chn, aligned_width, FRAME_HEIGHT, profile_name,
        ENCODER_BITRATE_KBPS, GOP_LENGTH))


def split_nalus(encoded_data):
    view = memoryview(encoded_data)
    data_length = len(encoded_data)
    start_code_pos = encoded_data.find(b"\x00\x00\x01")
    start_codes = []

    while start_code_pos >= 0 and start_code_pos + 3 <= data_length:
        prefix_start = start_code_pos - 1 if (
            start_code_pos > 0 and encoded_data[start_code_pos - 1] == 0
        ) else start_code_pos
        start_codes.append((prefix_start, start_code_pos + 3))
        start_code_pos = encoded_data.find(b"\x00\x00\x01", start_code_pos + 3)

    nalus = []
    for index, (_, payload_start) in enumerate(start_codes):
        payload_end = start_codes[index + 1][0] if index + 1 < len(start_codes) else data_length
        if payload_end > payload_start:
            nalus.append(view[payload_start:payload_end])
    return nalus


def make_rtp_timestamp(timestamp_ms):
    return (int(timestamp_ms) * RTP_TICKS_PER_MS) & 0xFFFFFFFF


def make_rtp_header(sequence_number, timestamp, marker, ssrc):
    second_byte = 0x60 | (0x80 if marker else 0)
    return struct.pack(
        ">BBHII", 0x80, second_byte,
        sequence_number & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc
    )


def packetize_nalu(nalu, timestamp, sequence_number, ssrc, output_packets):
    nalu_length = len(nalu)
    if nalu_length <= MAX_NALU_CHUNK_SIZE:
        output_packets.append(
            make_rtp_header(sequence_number, timestamp, False, ssrc) + bytes(nalu)
        )
        return (sequence_number + 1) & 0xFFFF

    fu_indicator = (nalu[0] & 0xE0) | 28
    nalu_type = nalu[0] & 0x1F
    offset = 1
    first_fragment = True

    while offset < nalu_length:
        chunk = nalu[offset:offset + MAX_NALU_CHUNK_SIZE]
        offset += MAX_NALU_CHUNK_SIZE
        last_fragment = offset >= nalu_length
        fu_header = nalu_type
        if first_fragment:
            fu_header |= 0x80
            first_fragment = False
        if last_fragment:
            fu_header |= 0x40
        output_packets.append(
            make_rtp_header(sequence_number, timestamp, False, ssrc)
            + bytes((fu_indicator, fu_header)) + bytes(chunk)
        )
        sequence_number = (sequence_number + 1) & 0xFFFF

    return sequence_number


def elapsed_ms(now_ms, previous_ms):
    if previous_ms is None:
        return None
    difference = now_ms - previous_ms
    return difference + TICKS_MS_PERIOD if difference < 0 else difference


def estimate_airtime_ms(frame_size_bytes, packet_count, link_kbps):
    wire_size = frame_size_bytes + packet_count * WIRE_OVERHEAD_BYTES
    return wire_size * 8.0 / link_kbps


class IdrRequestGate:
    def __init__(self, minimum_interval_ms, settle_interval_ms):
        self.minimum_interval_ms = minimum_interval_ms
        self.settle_interval_ms = settle_interval_ms
        self.last_idr_sent_ms = None
        self.last_request_granted_ms = None
        self.received = 0
        self.granted = 0
        self.merged_after_idr = 0
        self.merged_by_rate_limit = 0

    def note_idr_sent(self, now_ms):
        self.last_idr_sent_ms = now_ms

    def reset_stats(self):
        self.received = 0
        self.granted = 0
        self.merged_after_idr = 0
        self.merged_by_rate_limit = 0

    def request(self, now_ms):
        self.received += 1
        since_idr = elapsed_ms(now_ms, self.last_idr_sent_ms)
        if since_idr is not None and since_idr < self.settle_interval_ms:
            self.merged_after_idr += 1
            return False

        since_grant = elapsed_ms(now_ms, self.last_request_granted_ms)
        if since_grant is not None and since_grant < self.minimum_interval_ms:
            self.merged_by_rate_limit += 1
            return False

        self.last_request_granted_ms = now_ms
        self.granted += 1
        return True


class SendAborted(Exception):
    pass


def send_packet(sock, packet, stats):
    attempts = 0
    while True:
        attempts += 1
        try:
            sock.sendto(packet, (SERVER_IP, RTP_PORT))
        except TypeError:
            if attempts >= SEND_RETRY_LIMIT:
                raise SendAborted("Socket rejected packet type")
            packet = bytes(packet)
        except OSError as error:
            retryable_errors = (11, 115, 10035, 35)
            if attempts >= SEND_RETRY_LIMIT or getattr(error, "errno", None) not in retryable_errors:
                raise SendAborted(str(error))
            sleep_us(SEND_RETRY_DELAY_US)
        else:
            stats.bytes_tx += len(packet)
            stats.packets_tx += 1
            return packet


def send_frame_packets(packets, sock, stats, packet_ring, first_sequence):
    for index, packet in enumerate(packets):
        sent_packet = send_packet(sock, packet, stats)
        packet_ring.put((first_sequence + index) & 0xFFFF, sent_packet)


class PacketRing:
    def __init__(self, capacity=128):
        capacity = int(capacity)
        if capacity < 2 or capacity & (capacity - 1):
            raise ValueError("PacketRing capacity must be a power of two")
        self.mask = capacity - 1
        self.slots = [None] * capacity

    def put(self, sequence_number, packet):
        sequence_number &= 0xFFFF
        self.slots[sequence_number & self.mask] = (sequence_number, packet)

    def get(self, sequence_number):
        entry = self.slots[sequence_number & self.mask]
        if entry is not None and entry[0] == (sequence_number & 0xFFFF):
            return entry[1]
        return None


def retransmit_budget_bytes(bitrate_kbps, ratio=RETRANSMIT_BUDGET_RATIO,
                            window_ms=RETRANSMIT_WINDOW_MS):
    return int(bitrate_kbps * 1000.0 / 8.0 * ratio * window_ms / 1000.0)


class RetransmitLimiter:
    def __init__(self, limit_bytes, window_ms=RETRANSMIT_WINDOW_MS):
        self.limit_bytes = int(limit_bytes)
        self.window_ms = window_ms
        self.used_bytes = 0
        self.window_start_ms = None

    def allow(self, now_ms, packet_size):
        elapsed = elapsed_ms(now_ms, self.window_start_ms)
        if elapsed is None or elapsed >= self.window_ms:
            self.window_start_ms = now_ms
            self.used_bytes = 0
        if self.used_bytes + packet_size > self.limit_bytes:
            return False
        self.used_bytes += packet_size
        return True


def retransmit_nack(sequences, packet_ring, limiter, sock, now_ms, stats, command_link):
    hits = misses = budget_drops = errors = 0
    for sequence_number in sequences:
        packet = packet_ring.get(sequence_number)
        if packet is None:
            misses += 1
            continue
        if not limiter.allow(now_ms, len(packet)):
            budget_drops += 1
            continue
        try:
            sock.sendto(packet, (SERVER_IP, RTP_PORT))
        except OSError:
            errors += 1
            continue
        hits += 1
        stats.retransmitted_bytes += len(packet)

    command_link.nack_hits += hits
    command_link.nack_misses += misses
    command_link.nack_budget_drops += budget_drops
    stats.packet_errors += errors


class TxStats:
    def __init__(self):
        self.reset_rolling()
        self.gc_frames = 0
        self.last_frame_bytes = 0
        self.last_idr_packet_count = 0
        self.last_idr_airtime_ms = 0.0
        self.last_pframe_airtime_ms = 0.0

    def reset_rolling(self):
        self.sent_frames = 0
        self.dropped_frames = 0
        self.idr_frames = 0
        self.idr_bytes = 0
        self.bytes_tx = 0
        self.packets_tx = 0
        self.retransmitted_bytes = 0
        self.packet_errors = 0
        self.forced_idr_requests = 0
        self.max_frame_bytes = 0
        self.gc_window_us = 0
        self.gc_window_count = 0

    def note_gc(self, duration_us):
        self.gc_window_us += duration_us
        self.gc_window_count += 1

    def note_frame(self, frame_bytes, packet_count, is_idr, airtime_ms):
        self.sent_frames += 1
        self.gc_frames += 1
        self.last_frame_bytes = frame_bytes
        self.max_frame_bytes = max(self.max_frame_bytes, frame_bytes)
        if is_idr:
            self.idr_frames += 1
            self.idr_bytes += frame_bytes
            self.last_idr_packet_count = packet_count
            self.last_idr_airtime_ms = airtime_ms
        else:
            self.last_pframe_airtime_ms = airtime_ms

    def idr_share_percent(self):
        return 100.0 * self.idr_bytes / self.bytes_tx if self.bytes_tx else 0.0

    def format_line(self, interval_seconds, free_memory):
        bitrate_kbps = self.bytes_tx * 8 / interval_seconds / 1000.0
        fps = self.sent_frames / interval_seconds
        packets_per_second = self.packets_tx / interval_seconds
        retransmit_kbps = self.retransmitted_bytes * 8 / interval_seconds / 1000.0
        return (
            "stat: sent=%d drop=%d idr=%d (forced=%d) last=%dB max=%dB "
            "kbps=%.0f fps=%.1f pps=%.0f txerr=%d retx=%.1fkbps "
            "idrshare=%.0f%% gc=%.1fms/%d free=%d" % (
                self.sent_frames, self.dropped_frames, self.idr_frames,
                self.forced_idr_requests, self.last_frame_bytes,
                self.max_frame_bytes, bitrate_kbps, fps, packets_per_second,
                self.packet_errors, retransmit_kbps, self.idr_share_percent(),
                self.gc_window_us / 1000.0, self.gc_window_count, free_memory
            )
        )

    def format_detail(self, idr_gate):
        return (
            "      I-frame: %d packets, %.1fms | IDR requests: rx=%d granted=%d "
            "merged-after-IDR=%d rate-limited=%d" % (
                self.last_idr_packet_count, self.last_idr_airtime_ms,
                idr_gate.received, idr_gate.granted,
                idr_gate.merged_after_idr, idr_gate.merged_by_rate_limit
            )
        )


def crc16_ccitt_false(data):
    crc = 0xFFFF
    for byte in bytes(data):
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def valid_uart_frame(data):
    if data is None or len(data) != 15:
        return False
    if data[0] != 0xAA or data[1] != 0x55:
        return False
    if data[13] != 0x0D or data[14] != 0x0A:
        return False
    for index in (6, 7, 8):
        if data[index] not in (0, 1, 2):
            return False
    if data[9] not in (0, 1):
        return False
    if data[10] not in (0, 1):
        return False
    received = (data[11] << 8) | data[12]
    return crc16_ccitt_false(data[:11]) == received


def uart_tx_frame(serial_port, frame):
    data = bytes(frame)
    result = serial_port.write(data)
    return len(data) if result is None else int(result), len(data)


def init_uart():
    global uart
    from machine import UART, FPIOA
    pin_map = FPIOA()
    pin_map.set_function(32, FPIOA.UART3_TXD)
    pin_map.set_function(33, FPIOA.UART3_RXD)
    uart = UART(
        UART.UART3,
        baudrate=UART_BAUDRATE,
        bits=UART.EIGHTBITS,
        parity=UART.PARITY_NONE,
        stop=UART.STOPBITS_ONE,
    )
    print("[CMD] UART3 ready @%d" % UART_BAUDRATE)
    return uart


def init_motor_pwm():
    global motor_pwm, motor_dir_pin, motor_en_pin
    try:
        from machine import PWM, Pin
        # 对齐ZDT例程三线: 例程PA5=En恒低使能/PA6=PUL脉冲/PA7=Dir方向 -> K230: pin44=En, pin42=PWM0(PUL), pin43=GPIO(DIR)
        motor_en_pin = Pin(44, Pin.OUT, pull=Pin.PULL_NONE, drive=7)
        motor_en_pin.value(0)
        motor_pwm = PWM(42, freq=MOTOR_SPEED_HZ, duty=0)
        motor_dir_pin = Pin(43, Pin.OUT, pull=Pin.PULL_NONE, drive=7)
        motor_dir_pin.value(0)
        print("[MOTOR] fire pulse ready: PUL=pin42(PWM0) DIR=pin43 EN=pin44(En低=使能) %dHz 50%%" % MOTOR_SPEED_HZ)
    except Exception as error:
        motor_pwm = None
        motor_dir_pin = None
        motor_en_pin = None
        print("[MOTOR] init failed: %s" % error)


def set_motor_fire(fire):
    global motor_running
    if motor_pwm is None or motor_dir_pin is None:
        return
    wanted = bool(fire)
    if wanted == motor_running:
        return
    try:
        if wanted:
            motor_dir_pin.value(MOTOR_DIRECTION)
            motor_pwm.duty(50)
        else:
            motor_pwm.duty(0)
            motor_dir_pin.value(0)
        motor_running = wanted
    except Exception as error:
        motor_running = False
        print("[MOTOR] fire error: %s" % error)
        try:
            motor_pwm.duty(0)
        except Exception:
            pass


def stop_motor_pwm():
    global motor_pwm, motor_dir_pin, motor_en_pin
    if motor_pwm is not None:
        try:
            motor_pwm.duty(0)
            motor_pwm.deinit()
        except Exception:
            pass
        motor_pwm = None
    if motor_dir_pin is not None:
        try:
            motor_dir_pin.value(0)
            motor_dir_pin.deinit()
        except Exception:
            pass
        motor_dir_pin = None
    if motor_en_pin is not None:
        try:
            motor_en_pin.deinit()
        except Exception:
            pass
        motor_en_pin = None


def parse_nack(data):
    if data is None or len(data) < 5:
        return None, False
    if bytes(data[:3]) != CONTROL_MAGIC or data[3] != CTRL_NACK:
        return None, False

    sequence_count = data[4]
    if sequence_count < 1 or sequence_count > MAX_NACK_SEQUENCES:
        return None, False
    if len(data) != 5 + sequence_count * 2:
        return None, False

    sequences = []
    offset = 5
    for _ in range(sequence_count):
        sequences.append((data[offset] << 8) | data[offset + 1])
        offset += 2
    return sequences, True


class CommandLink:
    RECV_BUFFER_SIZE = 128
    SOCKET_TIMEOUT_SECONDS = 0.005
    UART_FRAME_LEN = 15

    def __init__(self, serial_port, on_control=None, on_nack=None):
        self.serial_port = serial_port
        self.active = False
        self.received_frames = 0
        self.last_receive_ms = 0
        self.on_control = on_control
        self.on_nack = on_nack
        self.cached_command = None
        self.cached_at_ms = 0
        self.last_uart_tx_ms = 0
        self.uart_tx_frames = 0
        self.nack_packets = 0
        self.nack_sequences = 0
        self.nack_hits = 0
        self.nack_misses = 0
        self.nack_budget_drops = 0
        self.nack_invalid = 0

    def _uart_write(self, frame):
        try:
            bytes_written, frame_length = uart_tx_frame(
                self.serial_port, frame)
        except Exception as error:
            print("[CMD] UART write error: %s" % error)
            self.active = False
            return False
        if bytes_written != frame_length:
            print("[CMD] UART short write %s/%d" % (
                bytes_written, frame_length))
            self.active = False
            return False
        self.uart_tx_frames += 1
        return True

    def _uart_tick(self, now_ms):
        if not self.last_uart_tx_ms:
            self.last_uart_tx_ms = now_ms
            return
        if ticks_diff(now_ms, self.last_uart_tx_ms) < UART_TX_PERIOD_MS:
            return
        self.last_uart_tx_ms = now_ms
        if self.cached_command is not None:
            frame = self.cached_command
        else:
            frame = UART_TX_IDLE_FRAME
        self._uart_write(frame)
        set_motor_fire(frame[9])

    def pump(self, sock):
        now_ms = ticks_ms()
        latest_command = None
        pending_nacks = []
        try:
            sock.settimeout(0)
            while True:
                data = sock.recv(self.RECV_BUFFER_SIZE)
                if not data:
                    break

                if len(data) >= 4 and bytes(data[:3]) == CONTROL_MAGIC:
                    if data[3] == CTRL_IDR:
                        if self.on_control:
                            self.on_control()
                        continue
                    if data[3] == CTRL_NACK:
                        sequences, valid = parse_nack(data)
                        if not valid:
                            self.nack_invalid += 1
                            continue
                        self.nack_packets += 1
                        self.nack_sequences += len(sequences)
                        pending_nacks.append(sequences)
                        continue

                if valid_uart_frame(data):
                    latest_command = data
        except OSError:
            pass
        finally:
            try:
                sock.settimeout(self.SOCKET_TIMEOUT_SECONDS)
            except Exception:
                pass

        if self.on_nack:
            for sequences in pending_nacks:
                try:
                    self.on_nack(sequences)
                except Exception as error:
                    self.nack_invalid += 1
                    print("[CMD] NACK handling error: %s" % error)

        if latest_command is not None:
            self.cached_command = bytes(latest_command)
            self.cached_at_ms = now_ms
            self.received_frames += 1
            self.last_receive_ms = now_ms
            if not self.active:
                print("[CMD] server streaming")
                self.active = True
        elif self.last_receive_ms and ticks_diff(now_ms, self.last_receive_ms) > COMMAND_STALE_MS:
            if self.active:
                print("[CMD] feed lost")
                self.active = False
            self.cached_command = None
            self.cached_at_ms = 0

        self._uart_tick(now_ms)

    def format_stats(self):
        return (
            "[CMD] rx=%d tx=%d cached=%s active=%s nack=%d seq=%d hit=%d miss=%d budget=%d bad=%d" % (
                self.received_frames, self.uart_tx_frames,
                "yes" if self.cached_command is not None else "no",
                self.active, self.nack_packets,
                self.nack_sequences, self.nack_hits, self.nack_misses,
                self.nack_budget_drops, self.nack_invalid
            )
        )


def stream_loop(sock, wlan):
    stream = StreamData()
    parameter_sets = None
    idr_gate = IdrRequestGate(IDR_REQUEST_INTERVAL_MS, IDR_SETTLE_MS)
    stats = TxStats()
    ssrc = 0x54494E44
    sequence_number = 0
    last_stats_time = time.time()
    idr_pending = [True]
    packet_ring = PacketRing()
    retransmit_limiter = RetransmitLimiter(retransmit_budget_bytes(ENCODER_BITRATE_KBPS))
    command_link = None

    def request_idr(_command=None):
        idr_pending[0] = True

    def handle_nack(sequences):
        retransmit_nack(
            sequences, packet_ring, retransmit_limiter, sock,
            ticks_ms(), stats, command_link
        )

    command_link = CommandLink(init_uart(), on_control=request_idr, on_nack=handle_nack)
    init_motor_pwm()

    while True:
        os.exitpoint()
        if not wlan.isconnected():
            wait_for_wifi(wlan)

        command_link.pump(sock)
        if idr_pending[0]:
            idr_pending[0] = False
            if idr_gate.request(ticks_ms()):
                encoder.RequestIDR()
                stats.forced_idr_requests += 1
                print("RTP: requested IDR")

        if encoder.GetStream(stream, timeout=4) != 0:
            command_link.pump(sock)
            continue

        frame_time_ms = ticks_ms()
        try:
            packets = []
            frame_size_bytes = 0
            is_idr = False
            next_sequence_number = sequence_number
            timestamp = make_rtp_timestamp(frame_time_ms)

            for index in range(stream.pack_cnt):
                frame_data = uctypes.bytearray_at(stream.data[index], stream.data_size[index])
                stream_type = stream.stream_type[index]
                if stream_type == encoder.STREAM_TYPE_HEADER:
                    parameter_sets = bytes(frame_data)
                    continue
                if stream_type == encoder.STREAM_TYPE_I:
                    is_idr = True

                frame_data = bytes(frame_data)
                frame_size_bytes += len(frame_data)
                if stream_type == encoder.STREAM_TYPE_I and parameter_sets:
                    for nalu in split_nalus(parameter_sets):
                        next_sequence_number = packetize_nalu(
                            nalu, timestamp, next_sequence_number, ssrc, packets
                        )
                for nalu in split_nalus(frame_data):
                    next_sequence_number = packetize_nalu(
                        nalu, timestamp, next_sequence_number, ssrc, packets
                    )

            if not packets:
                continue

            packets[-1] = bytearray(packets[-1])
            packets[-1][1] |= 0x80
            frame_airtime_ms = estimate_airtime_ms(
                frame_size_bytes, len(packets), WIFI_LINK_KBPS
            )

            if is_idr:
                idr_gate.note_idr_sent(ticks_ms())

            try:
                send_frame_packets(packets, sock, stats, packet_ring, sequence_number)
                stats.note_frame(frame_size_bytes, len(packets), is_idr, frame_airtime_ms)
            except SendAborted as error:
                stats.packet_errors += 1
                print("send error: %s" % error)

            sequence_number = next_sequence_number
        except Exception as error:
            print("send drop: %s" % error)
            stats.dropped_frames += 1
        finally:
            encoder.ReleaseStream(stream)

        if stats.gc_frames and stats.gc_frames % GC_COLLECT_EVERY == 0:
            gc_start_us = ticks_us()
            gc.collect()
            stats.note_gc(ticks_diff(ticks_us(), gc_start_us))

        now = time.time()
        if now - last_stats_time >= 10:
            try:
                free_memory = gc.mem_free()
            except Exception:
                free_memory = -1
            interval_seconds = max(1e-6, now - last_stats_time)
            print(stats.format_line(interval_seconds, free_memory)
                  + " (" + command_link.format_stats() + ")")
            print(stats.format_detail(idr_gate))
            bitrate_kbps = stats.bytes_tx * 8 / interval_seconds / 1000.0
            print("      P-frame airtime %.1fms / link usage %.0f%%" % (
                stats.last_pframe_airtime_ms,
                bitrate_kbps / WIFI_LINK_KBPS * 100.0
            ))
            if command_link.nack_packets or command_link.nack_misses:
                hit_count = command_link.nack_hits
                total_sequences = hit_count + command_link.nack_misses
                hit_rate = 100.0 * hit_count / total_sequences if total_sequences else 0.0
                print("      NACK %d packets/%d seq -> hit %d (%.0f%%), miss %d, "
                      "budget drops %d, invalid %d" % (
                    command_link.nack_packets, command_link.nack_sequences,
                    hit_count, hit_rate, command_link.nack_misses,
                    command_link.nack_budget_drops, command_link.nack_invalid
                ))

            last_stats_time = now
            stats.reset_rolling()
            idr_gate.reset_stats()
            command_link.nack_packets = 0
            command_link.nack_sequences = 0
            command_link.nack_hits = 0
            command_link.nack_misses = 0
            command_link.nack_budget_drops = 0
            command_link.nack_invalid = 0


def cleanup():
    global sensor, media_link, encoder
    stop_motor_pwm()
    if sensor is not None:
        try:
            sensor.stop()
        except Exception:
            pass
    if media_link is not None:
        try:
            media_link.destroy()
        except Exception:
            pass
        media_link = None
    if encoder is not None and encoder.chn >= 0:
        try:
            encoder.Stop()
            encoder.Destroy()
        except Exception:
            pass
        encoder = None


def create_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for option_name, buffer_size in (
        (getattr(socket, "SO_SNDBUF", None), 256 * 1024),
        (getattr(socket, "SO_RCVBUF", None), 4 << 20),
    ):
        if option_name is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, option_name, buffer_size)
            except Exception:
                pass

    try:
        sock.bind(("0.0.0.0", 0))
    except Exception:
        pass
    return sock


def main():
    wlan = init_wifi()
    print("[cfg] tier=%s %dx%d %dkbps | link=%dkbps | target=%s:%d" % (
        SELECTED_TIER, FRAME_WIDTH, FRAME_HEIGHT, ENCODER_BITRATE_KBPS,
        WIFI_LINK_KBPS, SERVER_IP, RTP_PORT
    ))

    init_camera()
    init_encoder()
    sock = create_socket()
    print("[4/4] RTP H.264 -> %s:%d (tier=%s, encoder=%dkbps, link=%dkbps, GOP=%d, chunk=%d)" % (
        SERVER_IP, RTP_PORT, SELECTED_TIER, ENCODER_BITRATE_KBPS,
        WIFI_LINK_KBPS, GOP_LENGTH, MAX_NALU_CHUNK_SIZE
    ))
    stream_loop(sock, wlan)


try:
    main()
except KeyboardInterrupt as error:
    print("User stop: %s" % error)
except BaseException as error:
    import sys
    sys.print_exception(error)
finally:
    cleanup()
    try:
        if uart is not None:
            uart.deinit()
    except Exception:
        pass
    try:
        if sta is not None:
            sta.active(False)
    except Exception:
        pass
    gc.collect()
