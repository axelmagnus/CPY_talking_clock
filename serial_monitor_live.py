import os
import time
import select
import termios

PORT = "/dev/tty.usbmodem2101"
DURATION_SECONDS = 540


INTERESTING = (
    "PERF ",
    "SYNC ",
    "STALL ",
    "GC ",
    "Traceback",
    "MemoryError",
    "Events in 24h",
    "sync_and_refresh",
)


def configure_port(fd):
    try:
        attrs = termios.tcgetattr(fd)
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        attrs[3] = attrs[3] & ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass


def main():
    fd = os.open(PORT, os.O_RDONLY | os.O_NONBLOCK)
    configure_port(fd)

    start = time.time()
    buf = b""
    lines = []

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
            raw_line, buf = buf.split(b"\n", 1)
            line = raw_line.decode("utf-8", "ignore").rstrip("\r")
            if not line:
                continue

            if any(token in line for token in INTERESTING):
                t = int(time.time() - start)
                out = "t=%4ds %s" % (t, line)
                print(out)
                lines.append(out)

    os.close(fd)
    print("--- CAPTURE_DONE lines=%d seconds=%d" % (len(lines), int(time.time() - start)))


if __name__ == "__main__":
    main()
