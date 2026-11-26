/*
  voa_player.ino: play 12-bit drive codes from the PC at exactly 1 kHz into the
  MEMS VOA drive circuit, and return the photodetector monitor on A0 for every
  sample. A 512-sample ring buffer keeps PC timing jitter away from the output.
  Host: python -m oclab.voa (src/oclab/voa). Protocol compatible with
  voa_dac_streamer.ino version 2.

  Board: Arduino Uno or Nano (ATmega328P), 115200 baud.
  OUTPUT_PWM 1 (default): D9 = Timer1 10-bit fast PWM at 15.625 kHz into
    1 kOhm/100 nF + 10 kOhm/10 nF, an LM358 follower and a 2N2222 emitter
    follower (8 V supply) driving VOA Positive. Level n = (code + 2) >> 2
    (0 to 1024) gives n/1024 of the 5V pin. n = 0 disconnects the timer and
    holds D9 low, so 0 V is a true 0 V.
  OUTPUT_PWM 0: MCP4725 DAC on SDA/SCL (replies 'E' if it is not found).

  D8: high from the first played sample to the end of playback.
  A0: photodetector INTENSITY output through 10 kOhm with Schottky clamps.

  The VOA is a bright type: 0 V gives the most light, so a reset gives full
  light. The host limits codes to its 5 V software limit; MAX_CODE below is a
  second, compile-time ceiling.

  PC to Arduino
    'R'                 stop, clear the buffer, reply 'K'
    'V' lo hi           set the output now (ignored while playing)
    'Q'                 read A0 16 times (only when idle), reply 'Q' a b
    'D' n (lo hi) x n   add n samples (1 to 64) to the buffer
    'G'                 start playback
    'F'                 finish when the buffer is empty

  Arduino to PC
    'K'                 ready
    'C'                 64 samples played: the PC may send 64 more
    0x80|(v>>7), v&0x7F one A0 sample v (0 to 1023), read just before each
                        output update, so sample k shows the light from output k-1
    'Q' a b             sum of 16 A0 reads = ((a & 0x7F) << 7) | b
    'E'                 MCP4725 selected but not found
    'Z' text '\n'       played underruns i2c_errors max_late_us monitor_dropped

  Bytes of 0x80 and above only ever start a monitor pair. Monitor bytes are
  sent only when the transmit buffer has room, so the 1 ms timing never waits.
*/

#define OUTPUT_PWM 1

#if !OUTPUT_PWM
#include <Wire.h>
#endif

const uint8_t PWM_PIN = 9;          // OC1A
const uint8_t SYNC_PIN = 8;
const uint8_t MONITOR_PIN = A0;
const uint32_t BAUD = 115200;
const uint32_t PERIOD_US = 1000;
const uint16_t BUFFER_SIZE = 512;   // Power of two
const uint16_t BUFFER_MASK = BUFFER_SIZE - 1;
const uint8_t MON_SIZE = 128;       // Power of two
const uint8_t MON_MASK = MON_SIZE - 1;
const uint8_t CHUNK = 64;
const uint16_t MAX_CODE = 4095;     // 100 % duty = the 5V pin (about 5 V)

uint16_t buffer[BUFFER_SIZE];
uint16_t head = 0, tail = 0, count = 0;

uint16_t monitor[MON_SIZE];
uint8_t monHead = 0, monTail = 0, monCount = 0;

uint8_t dacAddress = 0;
bool playing = false;
bool finishRequested = false;
uint32_t nextTick = 0;
uint32_t played = 0;
uint32_t underruns = 0;
uint32_t i2cErrors = 0;
uint32_t maxLateUs = 0;
uint32_t monDropped = 0;
uint8_t creditCount = 0;

enum ParserState { WAIT_COMMAND, WAIT_COUNT, WAIT_LOW, WAIT_HIGH, SET_LOW, SET_HIGH };
ParserState parserState = WAIT_COMMAND;
uint8_t samplesLeft = 0;
uint8_t lowByte_ = 0;               // lowByte is an Arduino macro

// ---------------------------------------------------------------- output

#if OUTPUT_PWM
bool writeOutput(uint16_t code) {
  if (code > MAX_CODE) code = MAX_CODE;
  uint16_t n = (code + 2) >> 2;           // 0 to 1024
  if (n == 0) {
    TCCR1A &= ~_BV(COM1A1);               // Disconnect the timer, hold D9 low
    digitalWrite(PWM_PIN, LOW);
  } else {
    OCR1A = n - 1;                        // OCR1A = 1023 is 100 % duty
    TCCR1A |= _BV(COM1A1);
  }
  return true;
}

bool initOutput() {
  pinMode(PWM_PIN, OUTPUT);
  digitalWrite(PWM_PIN, LOW);
  TCCR1A = _BV(WGM11) | _BV(WGM10);       // Fast PWM, 10-bit (with WGM12)
  TCCR1B = _BV(WGM12) | _BV(CS10);        // No prescaler: 16 MHz / 1024 = 15.625 kHz
  OCR1A = 0;
  return true;
}
#else
bool writeOutput(uint16_t code) {
  if (code > MAX_CODE) code = MAX_CODE;
  Wire.beginTransmission(dacAddress);
  Wire.write((uint8_t)(code >> 8));       // Fast mode, normal power
  Wire.write((uint8_t)(code & 0xFF));
  return Wire.endTransmission() == 0;
}

bool initOutput() {
  Wire.begin();
  Wire.setClock(400000UL);
  for (uint8_t address = 0x60; address <= 0x67; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      dacAddress = address;
      return true;
    }
  }
  return false;
}
#endif

// ---------------------------------------------------------------- buffers

void resetPlayback() {
  playing = false;
  finishRequested = false;
  head = tail = count = 0;
  monHead = monTail = monCount = 0;
  played = underruns = i2cErrors = maxLateUs = monDropped = 0;
  creditCount = 0;
  parserState = WAIT_COMMAND;
  digitalWrite(SYNC_PIN, LOW);
}

void pushSample(uint16_t code) {
  if (count >= BUFFER_SIZE) return;       // The credit scheme prevents this
  buffer[head] = code;
  head = (head + 1) & BUFFER_MASK;
  count++;
}

void pushMonitor(uint16_t value) {
  if (monCount >= MON_SIZE) {
    monDropped++;
    return;
  }
  monitor[monHead] = value;
  monHead = (monHead + 1) & MON_MASK;
  monCount++;
}

// Send queued monitor pairs without ever blocking the 1 kHz timing.
void drainMonitor(bool blocking) {
  // Keep room in the transmit buffer so the 'C' credit in tick() never waits.
  while (monCount > 0 && (blocking || Serial.availableForWrite() >= 4)) {
    uint16_t v = monitor[monTail];
    monTail = (monTail + 1) & MON_MASK;
    monCount--;
    Serial.write((uint8_t)(0x80 | (v >> 7)));
    Serial.write((uint8_t)(v & 0x7F));
  }
}

// ---------------------------------------------------------------- commands

void replyQuery() {
  uint16_t sum = 0;
  for (uint8_t i = 0; i < 16; i++) sum += analogRead(MONITOR_PIN);
  Serial.write('Q');
  Serial.write((uint8_t)(0x80 | (sum >> 7)));
  Serial.write((uint8_t)(sum & 0x7F));
}

void handleByte(uint8_t value) {
  switch (parserState) {
    case WAIT_COMMAND:
      if (value == 'R') {
        resetPlayback();
        Serial.write('K');
      } else if (value == 'V') {
        parserState = SET_LOW;
      } else if (value == 'Q' && !playing) {
        replyQuery();
      } else if (value == 'D') {
        parserState = WAIT_COUNT;
      } else if (value == 'G' && !playing) {
        playing = true;
        nextTick = micros();
      } else if (value == 'F') {
        finishRequested = true;
      }
      break;

    case WAIT_COUNT:
      samplesLeft = value;
      parserState = samplesLeft ? WAIT_LOW : WAIT_COMMAND;
      break;

    case WAIT_LOW:
      lowByte_ = value;
      parserState = WAIT_HIGH;
      break;

    case WAIT_HIGH:
      pushSample(((uint16_t)value << 8) | lowByte_);
      samplesLeft--;
      parserState = samplesLeft ? WAIT_LOW : WAIT_COMMAND;
      break;

    case SET_LOW:
      lowByte_ = value;
      parserState = SET_HIGH;
      break;

    case SET_HIGH:
      if (!playing) writeOutput(((uint16_t)value << 8) | lowByte_);
      parserState = WAIT_COMMAND;
      break;
  }
}

// ---------------------------------------------------------------- playback

void finishPlayback() {
  playing = false;
  finishRequested = false;
  digitalWrite(SYNC_PIN, LOW);
  drainMonitor(true);
  Serial.write('Z');
  Serial.print(played);
  Serial.print(' ');
  Serial.print(underruns);
  Serial.print(' ');
  Serial.print(i2cErrors);
  Serial.print(' ');
  Serial.print(maxLateUs);
  Serial.print(' ');
  Serial.println(monDropped);
}

void tick() {
  if (count == 0) {
    if (finishRequested) {
      finishPlayback();
      return;
    }
    underruns++;                          // Hold the last output until more samples arrive
    return;
  }

  if (played == 0) digitalWrite(SYNC_PIN, HIGH);

  pushMonitor(analogRead(MONITOR_PIN));   // About 0.11 ms: the light from the previous sample

  uint16_t code = buffer[tail];
  tail = (tail + 1) & BUFFER_MASK;
  count--;

  if (!writeOutput(code)) i2cErrors++;
  played++;

  if (++creditCount >= CHUNK) {
    creditCount = 0;
    Serial.write('C');
  }
}

void setup() {
  pinMode(SYNC_PIN, OUTPUT);
  digitalWrite(SYNC_PIN, LOW);
  pinMode(LED_BUILTIN, OUTPUT);

  Serial.begin(BAUD);
  analogRead(MONITOR_PIN);                // The first conversion after power-up is slow

  if (!initOutput()) {
    while (true) {
      Serial.write('E');
      digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
      delay(250);
    }
  }
  Serial.write('K');
}

void loop() {
  while (Serial.available() > 0) {
    handleByte((uint8_t)Serial.read());
  }

  if (playing) {
    uint32_t now = micros();
    int32_t late = (int32_t)(now - nextTick);
    if (late >= 0) {
      if ((uint32_t)late > maxLateUs) maxLateUs = (uint32_t)late;
      nextTick += PERIOD_US;
      tick();
    }
  }

  drainMonitor(false);
}
