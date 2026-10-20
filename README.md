# KICKR Coach

Local-first indoor cycling coach for a Wahoo KICKR CORE 2, with structured workouts, BLE/FTMS trainer control, Apple Watch heart-rate input via HeartCast, safety controls, session recording, and longitudinal training analysis.

## Project principle

KICKR Coach is not an HR-chasing controller. Power prescription is the primary training control; heart rate, cadence and RPE are monitoring and analysis signals. Most training adaptation happens between sessions rather than continuously changing power during a workout.

## Initial target platform

- macOS host
- Python 3.12+
- Wahoo KICKR CORE 2 over Bluetooth Low Energy / FTMS
- Apple Watch heart rate via HeartCast on iPhone, exposed as a BLE heart-rate source
- Local mobile-first web UI for iPhone/iPad/Mac
- SQLite + CSV export
- Fully local/offline operation

## Delivery strategy

Development is milestone-driven. The first milestone is a hardware feasibility spike only: discover and connect to KICKR + HeartCast concurrently, read KICKR power/cadence and HeartCast HR for at least 10 minutes, inspect FTMS capabilities, and verify safe acquisition/release of trainer control. Do not build the full application before this is proven.

See:

- `SPEC.md` — functional/non-functional requirements
- `ARCHITECTURE.md` — architecture and design decisions
- `AGENTS.md` — implementation instructions for Codex
- `MILESTONES.md` — phased delivery and acceptance criteria

## Safety

This project is training software, not a medical device. No automatic training decision may be based on a single HR sample. Safety and explicit user commands always override workout/adaptive logic.
