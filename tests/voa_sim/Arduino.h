// Minimal Arduino API for compiling firmware/voa_player/voa_player.ino on the
// host (see sim.cpp). Only what the sketch uses is provided.
#pragma once
#include <stddef.h>
#include <stdint.h>

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define A0 14
#define LED_BUILTIN 13
#define _BV(bit) (1u << (bit))

// ATmega328P Timer1 bits used by the sketch
#define WGM10 0
#define WGM11 1
#define COM1A1 7
#define CS10 0
#define WGM12 3
extern uint8_t TCCR1A, TCCR1B;
extern uint16_t OCR1A;

uint32_t micros();
void delay(unsigned long ms);
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t value);
int digitalRead(uint8_t pin);
int analogRead(uint8_t pin);

class SimSerial {
 public:
  void begin(unsigned long baud);
  int available();
  int read();
  int availableForWrite();
  size_t write(uint8_t value);
  size_t print(char value);
  size_t print(unsigned int value);
  size_t print(unsigned long value);
  size_t println(unsigned int value);
  size_t println(unsigned long value);

 private:
  size_t text(const char *s);
};
extern SimSerial Serial;
