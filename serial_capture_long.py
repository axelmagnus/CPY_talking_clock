import os
import time
import select
import termios

PORT = "/dev/tty.usbmodem2101"
DURATION_SECONDS = 390


def configure_port(fd):
    try:
        attrs = termios.tcgetattr(fd)
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        attrs[3] = attrs[3] & ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass


def parse_perf_line(line):
    if not line.startswith("PERF free="):
        return None
    fields = {}
    for token in line.split()[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    try:
        return {
            "free": int(fields.get("free", "0")),
            "hz": int(fields.get("hz", "0")),
            "dt_max_ms": int(fields.get("dt_max_ms", "0")),
            "raw": line,
        }
    except Exception:
        return None


def main():
    fd = os.open(PORT, os.O_RDONLY | os.O_NONBLOCK)
    configure_port(fd)

    start = time.time()
    buf = b""
    perf = []

    while time.time() - start < DURATION_SECONDS:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not data:
            continue
        buf += data

        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "ignore").strip("\r")
            parsed = parse_perf_line(text)
            if parsed is None:
                continue
            parsed["t"] = time.time() - start
            perf.append(parsed)

    os.close(fd)

    print("CAPTURE_SECONDS", int(time.time() - start), "PERF_COUNT", len(perf))
    if not perf:
        return

    frees = [p["free"] for p in perf]
    hzs = [p["hz"] for p in perf]
    dts = [p["dt_max_ms"] for p in perf]

    print("FREE min=%d max=%d last=%d" % (min(frees), max(frees), frees[-1]))
    print("HZ   min=%d max=%d last=%d" % (min(hzs), max(hzs), hzs[-1]))
    print("DT   min=%d max=%d last=%d" % (min(dts), max(dts), dts[-1]))

    by_minute = {}
    for p in perf:
        minute = int(p["t"] // 60)
        by_minute.setdefault(minute, []).append(p)

    print("PER_MINUTE")
    for minute in sorted(by_minute):
        arr = by_minute[minute]
        free_avg = sum(x["free"] for x in arr) // len(arr)
        hz_avg = sum(x["hz"] for x in arr) // len(arr)
        dt_avg = sum(x["dt_max_ms"] for x in arr) // len(arr)
        dt_max = max(x["dt_max_ms"] for x in arr)
        free_min = min(x["free"] for x in arr)
        print(
            "m%02d n=%d free_avg=%d free_min=%d hz_avg=%d dt_avg=%d dt_max=%d"
            % (minute, len(arr), free_avg, free_min, hz_avg, dt_avg, dt_max)
        )

    print("LAST_LINES")
    for p in perf[-12:]:
        print("t=%4ds %s" % (int(p["t"]), p["raw"]))


if __name__ == "__main__":
    main()
