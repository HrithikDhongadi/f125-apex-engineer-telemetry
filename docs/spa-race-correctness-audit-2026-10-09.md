# Spa race correctness audit — 2026-10-09

Primary dataset: `session-20261008T090243-5dd3a5b6`  
Corrected report: `data/reports/session-20261008T090243-5dd3a5b6-race-corrected.md`

This audit distinguishes immutable received data, final-timeline reconstruction, and engineering inference. No packet database, lap sample, official time, validity flag, session identity, or PB was rewritten.

## Dataset inventory

- Primary race: five lap JSON files, `session.json`, and a 205,450-row `race-data.sqlite3` packet store are available.
- Latest race report: `/home/hrithik/Downloads/Race-Spa-race.md`.
- Original race report: `/home/hrithik/Downloads/Race-Spa-race/report.md`. The task called this `report(1).md`; no file with that exact name was supplied.
- Original Time Trial report: `/home/hrithik/Downloads/Spa-TT-1-time-trial(1).md`; source session `session-20261008T110542-09947196` is available.
- Aggressive-setup Time Trial report: `/home/hrithik/Downloads/Spa-TT-4-time-trial.md`; source session `session-20261008T131532-a60d43a1` is available.
- The old and newer race reports are two processings of the same recording, not independent drives.

## Pipeline and affected modules

`TelemetryReceiver` receives datagrams; `protocol.py` validates and decodes F1 25 packet structures; `receiver.py` maintains live/session/lap state and writes lap JSON; `race_store.py` appends full decoded packets and timeline rows to SQLite; `analysis.py` performs distance alignment and corner-window analysis; `reports.py` creates Markdown/ZIP outputs; `circuit_profiles.py` manages profile calibration and provenance.

The correction is deliberately a read-time reconstruction over the immutable packet table. Historical reports can therefore be regenerated with newer processing versions even though early timeline rows did not contain frame IDs themselves. Those rows are correlated back to their source packets; failures remain `unknown` rather than being silently accepted.

## Root-cause analysis

| Issue | Root cause and evidence | Severity | Confidence | Correction |
|---|---|---:|---:|---|
| Flashback events counted twice | Timeline rows were sorted by rewound `session_time` and had no branch model. At overall frame 4530 the `FLBK` packet targets frame 3965/session time 60.578 s; the next packet rewinds `frameIdentifier` to 3965 while `overallFrameIdentifier` remains 4530. | Critical | High | Reconstruct in packet receive/overall-frame order; preserve branches and mark events `accepted`, `superseded`, or `unknown` with references/reasons. |
| 11 reported times overtaken | Seven player-targeted OVTK events at 63.730–68.568 s belong to the branch later rewound to 60.578 s. | Critical | High | Exclude superseded events from accepted statistics and reconcile each player OVTK notification against surrounding all-car Lap Data. |
| Warning reported as penalty | Report counted every player PENA packet as a penalty. Official F1 25 type 5 means Warning; infringement 4 means Small Collision. Lap Data changes warnings 0→1 while penalty seconds remain 0→0. | High | High | Decode notification class separately; report warnings, infringement notifications, actual-penalty-type notifications, final time-penalty counter, drive-throughs, stop-go events, and superseded notifications. |
| Collision count treated as incidents | Every player COLL message was described as a collision count, without branch validity or repeat grouping. One of 11 player messages is on the abandoned branch. | High | High for accepted messages; medium for physical incidents | Preserve raw messages, exclude the superseded message from final statistics, and conservatively group accepted messages only when pair/branch/time/location agree. |
| Lap 1 stale setup | The recorder froze the first setup too early. Frames 0/30/60 contain the 10 kg provisional setup; frame 90 changes to the 17 kg setup before start light 2, then repeats unchanged for 1,141 setup packets. | High | High | Audit full Packet 5 history. Keep the stored Lap 1 snapshot visible as superseded and use the repeated selection as reconstructed active context without rewriting lap JSON. |
| 0/0 race wings suspicious | All 1,144 race Packet 5 player records decode 0/0, while later TT data decodes non-zero wings. This is not a byte-offset-only failure. | Medium | High that 0/0 was transmitted; low on why | Retain 0/0 exactly and label verification `unverified_as_transmitted_zero`; do not substitute guessed values. |
| Frame gaps labelled packet loss | The old report summed unlike streams; the newer calculation fixed that but still named every ID jump “missing.” Largest gaps are simultaneous across all 60 Hz streams and game/session clocks jump 0.5–0.65 s while recorder wall time advances only 1–3 ms. | Medium | High | Report per-stream received/unique/duplicate/out-of-order frames and classify gaps. This recording has 164 worst-stream sequence-gap frames, 164 classified sequence discontinuities, and zero wall-clock-supported capture-loss frames. |
| Profile “verified” overstates calibration | A global label coexists with unverified individual turns, draft names, estimated apexes and overlapping windows. | Medium | High | Expose separate track identity, game-measured length, turn-label, boundary, and apex-calibration states. Keep corner analysis exploratory and deltas non-additive. |
| Corner analysis described where, not why | Existing windows provided delta/minimum speed but omitted several input/vehicle-state comparisons. | Medium | High | Add entry speed, brake onset/peak/release, gear, turn-in, apex/minimum/exit speed, throttle pickup/full-throttle, brake-throttle overlap, steering-correction indicator, lateral/longitudinal acceleration when present, rear slip and ERS when present. Avoid causal balance diagnoses. |

## Spa before/after reconciliation

| Statistic | Previous report | Corrected result | Evidence |
|---|---:|---:|---|
| Completed laps | 5 | 5 | Full-distance lap files 1–5 retained. |
| Fastest lap | 1:49.511 | 1:49.511 | Official Lap Data/lap JSON, lap 3. |
| Race movement | P14→P9 | P14→P9 | Lap-boundary position snapshots. No Final Classification packet was stored. |
| Raw player OVTK notifications | 9 made / 11 received | 9 made / 11 received | Raw evidence is unchanged. |
| Confirmed accepted passes | Not distinguished | 9 made / 4 received | All 13 accepted player OVTK events have corroborating player/relative position transitions; seven received notifications are superseded. |
| Player collision messages | 11 | 10 accepted + 1 superseded | Branch reconstruction. The 10 accepted messages form 10 conservative incidents under the one-second/pair/location rule. |
| Player penalties | 1 | 0 confirmed applied | The sole accepted player PENA is Warning/Small Collision; final accepted penalty counter is 0 s. |
| Player warnings | Not separated | 1 | PENA type 5 and Lap Data warnings 0→1. |
| Worst-stream frame gap estimate | 164 (0.478%) called missing | 164 sequence-gap frames; 0 supported capture-loss frames | Per-stream overall-frame, session-time, and recorder-wall-time comparison. |
| Lap 1 setup | Old 10 kg snapshot treated as lap setup | Original retained as superseded; repeated 17 kg setup is reconstructed active context | Player Packet 5 history and pre-lights timing. |

Net movement of five places does not imply five overtakes. The accepted record includes repeated passes and repasses plus position changes elsewhere in the field. The report therefore keeps raw notifications, confirmed manoeuvres, and net movement separate.

## Flashback evidence

The source packet at overall frame 4530 carries `FLBK(frame=3965, session_time=60.577858)`. Packets received before it at session times above the target are the abandoned version of the track section. The next Lap Data packet has session time 60.677, `frameIdentifier=3965`, and `overallFrameIdentifier=4530`; this is the replacement branch. Raw branch-A events remain queryable and exported with `superseded_by` and `flashback_reference`. Accepted branch-B events retain branch ID `branch-1`.

The recorder's completed lap remains valid because its per-lap sample path already pruned the abandoned samples and followed the game's validity flag. Continuous race statistics were the broken layer, not this session's saved lap boundary.

## Setup evidence

Both setup variants are Packet 5 records for player car index 19. The setup changes at frame 90 while session time is still 0.000 s; start light 1 was at frame 48, start light 2 at frame 95, and lights out at frame 329. The second setup then remains stable through the race. That makes the first variant provisional/superseded with high confidence.

The stored 0/0 wings cannot be externally confirmed from this dataset. They remain visible exactly as transmitted. Configured starting fuel remains distinct from instantaneous Car Status fuel.

## Continuity evidence

The report does not sum gaps across packet types. Motion, Lap Data, Car Telemetry, Car Status, and Motion Ex are tracked independently. The largest apparent gaps align across the streams. For example, Lap Data jumps overall frame 7949→7987 and session time 117.750→118.384 s, but stored receive time advances only about 2 ms. This proves a game/sequence discontinuity in the observed data, not a 0.634 s receiver outage. A future gap whose recorder wall clock advances with the expected missing-frame duration is classified `capture_loss`; ambiguous cases remain `unknown_gap`.

## Automated validation

The full suite passes 96 tests. Added coverage verifies:

- flashback branch preservation and accepted/superseded classification;
- source-packet frame/overall-frame provenance;
- OVTK confirmation using surrounding participant positions;
- the recorded Spa result of 9/4 confirmed manoeuvres;
- warning type 5 / small-collision infringement type 4 without an actual penalty;
- raw collision messages versus conservative incident grouping;
- collision events discarded by a later flashback;
- stable active-setup selection while preserving provisional history;
- per-stream continuity and low-rate stream exclusion;
- event attribution using the source packet's player index.

Existing regressions continue to cover normal race capture, standing-grid lap 1, session restart/UID isolation, race completion duplication, TT invalid-lap preservation/PB exclusion, Restart Lap, mid-lap and boundary flashbacks, historical setup compatibility, overlapping profile windows, and uncertainty for unavailable data.

Secondary raw-session validation also succeeded. Spa TT 1 contains five near-full-distance laps, preserves invalid lap 1, and retains its transmitted 0/0 wing setup. Spa TT 4 contains seven near-full-distance laps, preserves invalid laps 13/14/16, and consistently decodes the non-zero 6/2 wing setup. This supports the conclusion that the setup decoder can read non-zero wings and that the race's persistent 0/0 values must be treated as transmitted-but-unverified rather than “fixed” by changing offsets.

Checks executed:

```text
python3 -m unittest discover -s tests -v  -> 96 passed
python3 -m py_compile src/f1telemetry/*.py scripts/reprocess_session.py -> passed
npm run check:frontend -> passed
```

## Remaining limitations

- No Packet Final Classification (ID 8) exists in the primary database, so P14→P9 is supported by accepted lap-position snapshots rather than a stored post-race classification packet.
- Physical collision incident identity cannot be proven from COLL alone. The report exposes the grouping rule and both counts.
- The 0/0 wing values cannot be validated against a setup screenshot; changing them would fabricate data.
- Historical event rows require deterministic correlation back to packet rows. All primary Spa key events correlate; uncorrelated rows are explicitly `unknown`.
- The Spa turn profile is not ready for authoritative corner coaching. Track identity and game length are supported, but individual names/directions/boundaries/apexes need in-game trace calibration. Overlapping windows cannot be summed.
- Older lap JSON lacks per-sample ERS and some state channels even where slower raw packets exist. The report leaves unsupported corner metrics unavailable rather than interpolating them without a defined correlation method.
- The large SQLite database is caused by full decoded JSON for every car at 60 Hz plus indexes/WAL. Correctness is preserved, but a future version should use normalized/binary or compressed immutable packet storage and background batch commits after benchmarking crash guarantees.

## Recommended next engineering work

1. Calibrate Spa turns from several clean F1 25 Motion X/Z laps, verify every label/direction/window independently, and version the profile rather than overwriting its provenance.
2. Add a background historical materialization/cache for reconstructed events so repeated dashboard loads do not repeatedly parse large JSON payloads; keep the packet DB authoritative.
3. Correlate historical Car Status/Motion Ex packets into distance windows with explicit freshness limits, enabling defensible ERS, yaw, and acceleration comparisons without pretending slow streams are synchronous.
4. Capture a race with a stored Final Classification packet, pit stop, and safety-car/VSC period to validate final penalties, pit-derived position gains, and neutralized-race gaps.
5. Capture another race flashback with multiple event types and a flashback across a lap boundary to extend recorded-data validation beyond the current synthetic boundary regressions.
