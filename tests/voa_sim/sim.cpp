// Host simulation of the VOA player board, for tests/test_voa_firmware.py.
//
//   sim EVENTS_FILE [identity|bright]
//
// Compiled together with firmware/voa_player/voa_player.ino and Arduino.h. The
// board's serial port is a pseudo-terminal whose name is printed on stdout, so
// the host software talks to it exactly as to /dev/ttyUSB0. Serial runs at the
// real 115200 baud in both directions, with the ATmega's 63-byte transmit
// buffer, so Serial.availableForWrite() behaves as on the board. analogRead()
// takes 110 us and returns a "light" level computed from the Timer1 output:
//   identity  A0 = PWM level n (0 to 1024, capped at 1023)
//   bright    A0 falls with n, like a bright-type VOA; SIGUSR1 toggles a
//             blocked beam (A0 = 2), for the dark reading of a calibration.
// While playing, every A0 read logs "L n" (the output level being measured),
// D8 changes log "D8 value reads_so_far", and exit logs "END level d8".

#include "Arduino.h"

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include <deque>

uint8_t TCCR1A = 0, TCCR1B = 0;
uint16_t OCR1A = 0;
SimSerial Serial;

extern bool playing;  // The sketch's own state
void setup();
void loop();

static const uint64_t BYTE_NS = 10ull * 1000000000ull / 115200ull;  // 8N1
static const size_t TX_CAPACITY = 63;
static const size_t RX_CAPACITY = 64;

static int master_fd = -1;
static FILE *events = nullptr;
static uint64_t start_ns = 0;
static volatile sig_atomic_t stop_requested = 0;
static volatile sig_atomic_t beam_blocked = 0;
static bool bright = false;
static uint8_t pins[32];
static unsigned long play_reads = 0;
static std::deque<uint8_t> tx;
static uint64_t tx_free_ns = 0;  // When the byte at the front of tx has been sent
static std::deque<uint8_t> rx;
static uint64_t rx_clock_ns = 0;

static uint64_t now_ns() {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void busy_us(uint64_t us) {
  uint64_t end = now_ns() + us * 1000ull;
  while (now_ns() < end) {
  }
}

uint32_t micros() { return (uint32_t)((now_ns() - start_ns) / 1000ull); }
void delay(unsigned long ms) { busy_us(ms * 1000ull); }
void pinMode(uint8_t, uint8_t) {}
int digitalRead(uint8_t pin) { return pins[pin]; }

void digitalWrite(uint8_t pin, uint8_t value) {
  if (pin == 8 && pins[8] != value) fprintf(events, "D8 %d %lu\n", value, play_reads);
  pins[pin] = value;
}

static int output_level() {
  if (TCCR1A & _BV(COM1A1)) return OCR1A + 1;
  return pins[9] ? 1024 : 0;
}

int analogRead(uint8_t) {
  busy_us(110);
  int n = output_level();
  int value;
  if (!bright) {
    value = n > 1023 ? 1023 : n;
  } else {
    value = beam_blocked ? 2 : 2 + (int)lround(900.0 * exp(-n / 180.0));
  }
  if (playing) {
    fprintf(events, "L %d\n", n);
    play_reads++;
  }
  return value;
}

// ---------------------------------------------------------------- serial

static void tx_pump() {
  uint64_t t = now_ns();
  while (!tx.empty() && t >= tx_free_ns) {  // The front byte has left the line
    uint8_t b = tx.front();
    tx.pop_front();
    if (write(master_fd, &b, 1) < 0 && errno != EAGAIN) {
      // Nobody listening: the byte is lost, as on a real wire.
    }
    if (!tx.empty()) tx_free_ns += BYTE_NS;
  }
}

static void rx_pump() {
  uint64_t t = now_ns();
  if (rx.size() >= RX_CAPACITY) {
    rx_clock_ns = t;
    return;
  }
  size_t allowed = (size_t)((t - rx_clock_ns) / BYTE_NS);
  if (allowed == 0) return;
  if (allowed > RX_CAPACITY - rx.size()) allowed = RX_CAPACITY - rx.size();
  uint8_t data[RX_CAPACITY];
  ssize_t got = read(master_fd, data, allowed);
  if (got > 0) {
    for (ssize_t i = 0; i < got; i++) rx.push_back(data[i]);
    rx_clock_ns += (uint64_t)got * BYTE_NS;
  }
  if (got < (ssize_t)allowed) rx_clock_ns = t;  // Idle line: no credit builds up
}

void SimSerial::begin(unsigned long) {}

int SimSerial::available() {
  rx_pump();
  return (int)rx.size();
}

int SimSerial::read() {
  rx_pump();
  if (rx.empty()) return -1;
  int b = rx.front();
  rx.pop_front();
  return b;
}

int SimSerial::availableForWrite() {
  tx_pump();
  return (int)(TX_CAPACITY - tx.size());
}

size_t SimSerial::write(uint8_t value) {
  tx_pump();
  while (tx.size() >= TX_CAPACITY) tx_pump();  // Blocks, like HardwareSerial
  if (tx.empty()) tx_free_ns = now_ns() + BYTE_NS;
  tx.push_back(value);
  return 1;
}

size_t SimSerial::text(const char *s) {
  size_t n = 0;
  while (*s) n += write((uint8_t)*s++);
  return n;
}

size_t SimSerial::print(char value) { return write((uint8_t)value); }
size_t SimSerial::print(unsigned long value) {
  char s[24];
  snprintf(s, sizeof s, "%lu", value);
  return text(s);
}
size_t SimSerial::print(unsigned int value) { return print((unsigned long)value); }
size_t SimSerial::println(unsigned long value) { return print(value) + text("\r\n"); }
size_t SimSerial::println(unsigned int value) { return println((unsigned long)value); }

// ---------------------------------------------------------------- main

static void on_stop(int) { stop_requested = 1; }
static void on_block(int) { beam_blocked = !beam_blocked; }

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: %s EVENTS_FILE [identity|bright]\n", argv[0]);
    return 2;
  }
  events = fopen(argv[1], "w");
  bright = argc > 2 && strcmp(argv[2], "bright") == 0;
  master_fd = posix_openpt(O_RDWR | O_NOCTTY);
  if (!events || master_fd < 0 || grantpt(master_fd) || unlockpt(master_fd)) {
    perror("pty");
    return 1;
  }
  const char *name = ptsname(master_fd);
  int keep = open(name, O_RDWR | O_NOCTTY);  // Keeps the pty alive between host connections
  termios settings;
  tcgetattr(keep, &settings);
  cfmakeraw(&settings);
  tcsetattr(keep, TCSANOW, &settings);
  fcntl(master_fd, F_SETFL, O_NONBLOCK);
  signal(SIGTERM, on_stop);
  signal(SIGINT, on_stop);
  signal(SIGUSR1, on_block);

  start_ns = now_ns();
  tx_free_ns = rx_clock_ns = start_ns;
  printf("%s\n", name);
  fflush(stdout);

  setup();
  while (!stop_requested) {
    loop();
    tx_pump();
  }
  fprintf(events, "END %d %d\n", output_level(), pins[8]);
  fclose(events);
  close(keep);
  return 0;
}
