# 02 Five-report model test

Synthetic Taglish reports from `tests/fixtures/five_reports.json`, run by `python -m silong.bench`.
Each run unloads loaded models first, then times one warm-up request (cold load) and each extraction.
`llm_ms` is wall-clock time around one `extract.extract` call, measured on the machine running the bench.

Field scoring: `location` matches when equal after lowercasing and removing punctuation ("partial" when one
contains the other, not counted as a match); `injury_note` scores only whether a note is present; `summary`
is shown but not scored; other fields must be equal. Priority is computed by `silong.rules.classify`.

Rescored 2026-10-09 under review comment R20: report 3's expected `on_behalf` was corrected from false to true (the reporter is with the group, but only the grandfather needs medicine). Only that expected value, its match and the affected totals changed (`qwen3:4b` 36/40 → 37/40; `gemma4:e2b` 35/40 → 34/40). Model outputs, priorities and measured times are unchanged; no model was rerun.

## `qwen3:4b`, run 2026-10-09T09:57:24-07:00

### Environment

- Ollama: http://172.23.48.1:11434, version 0.40.2 (from `/api/version`)
- Model: `qwen3:4b`, digest `359d7dd4bcda`, 2,497,293,931 bytes, 4.0B, Q4_K_M (from `/api/tags`)
- CPU: AMD Ryzen 5 2400G with Radeon Vega Graphics
- Ollama host OS: Microsoft Windows 11 Pro 10.0.26200
- Bench OS: Ubuntu 26.04.1 LTS, kernel 6.18.33.2-microsoft-standard-WSL2; 8 logical CPUs visible
- Loaded before unload (`/api/ps`): none; after unload: none
- Windows memory before warm-up: total 14,258 MiB, free 5,192 MiB
- Bench machine memory before warm-up: total 6,894 MiB, available 3,298 MiB
- Cold load (one warm-up request): 13,706 ms (13.7 s)
- Loaded after run (`/api/ps`): `qwen3:4b`, size 3,184,001,022 bytes, size_vram 0, context 4096
- Windows memory after run: total 14,258 MiB, free 2,326 MiB

### Summary

| # | Report | Expected priority | Actual priority | Scored fields matched | llm_ms |
|---|---|---|---|---|---|
| 1 | outside-rescue-for-others | critical | critical | 8/8 | 63,933 |
| 2 | injury-in-center | critical | critical | 6/8 | 19,048 |
| 3 | vulnerable-supply-need | high | high | 8/8 | 19,303 |
| 4 | other-need | medium | medium | 7/8 | 16,038 |
| 5 | no-need | low | low | 8/8 | 17,162 |

Priority matched: 5/5. Scored fields matched: 37/40. Failed extractions: 0.
Successful `llm_ms`: min 16,038, median 19,048, max 63,933, total 135,484.

### 1. outside-rescue-for-others

> Paki-rescue po yung pamilya sa tapat ng bahay namin sa Sitio Malinis, Brgy. Bagong Silang. Apat sila nasa second floor, may buntis. Hanggang dibdib na ang baha.

- Expected priority: critical (Critical: rescue needed outside center)
- Actual priority: critical (Critical: rescue needed outside center)
- llm_ms: 63,933

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Sitio Malinis, Brgy. Bagong Silang | Sitio Malinis, Brgy. Bagong Silang | match |
| in_center | false | false | match |
| on_behalf | true | true | match |
| headcount | 4 | 4 | match |
| vulnerable | ["pregnant"] | ["pregnant"] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | ["rescue"] | ["rescue"] | match |
| summary | A family of four including a pregnant woman is trapped on a second floor by chest-high flood. | Family of four on second floor at Sitio Malinis, Bagong Silang with a pregnant person are trapped by floodwater up | not scored |

### 2. injury-in-center

> Nandito kami sa Gym ng evacuation center. Dalawa kaming nahiwa sa yero habang naglilinis, dumudugo pa yung kamay ko.

- Expected priority: critical (Critical: injury reported)
- Actual priority: critical (Critical: medical need)
- llm_ms: 19,048

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Gym, evacuation center | Gym, evacuation center | match |
| in_center | true | true | match |
| on_behalf | false | false | match |
| headcount | 2 | null | no |
| vulnerable | [] | [] | match |
| injured | true | true | match |
| injury_note | cuts from metal sheet, hand still bleeding | hand bleeding from a fall | match |
| needs | [] | ["medical"] | no |
| summary | Two people in the gym were cut by metal sheeting while cleaning; one hand is still bleeding. | Two people are bleeding from a fall in the gym; reporter needs medical help. | not scored |

### 3. vulnerable-supply-need

> Sa Room 7 po ng evacuation center, tatlo kami dito. Yung lolo namin 80 years old, ubos na yung maintenance na gamot niya para sa high blood.

- Expected priority: high (High: senior + medicine)
- Actual priority: high (High: senior + medicine)
- llm_ms: 19,303

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Room 7, evacuation center | Room 7, evacuation center | match |
| in_center | true | true | match |
| on_behalf | true | true | match |
| headcount | 3 | 3 | match |
| vulnerable | ["senior"] | ["senior"] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | ["medicine"] | ["medicine"] | match |
| summary | Family of three in Room 7; their 80-year-old grandfather has run out of blood pressure medicine. | Three people in Room 7, evacuation center include an 80-year-old senior with no blood pressure medication. | not scored |

### 4. other-need

> Taga-covered court po kami sa evacuation center, walo kami dito. Kulang na po ang kumot at banig, malamig na sa gabi.

- Expected priority: medium (Medium: needs other)
- Actual priority: medium (Medium: needs other)
- llm_ms: 16,038

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | covered court, evacuation center | null | no |
| in_center | true | true | match |
| on_behalf | false | false | match |
| headcount | 8 | 8 | match |
| vulnerable | [] | [] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | ["other"] | ["other"] | match |
| summary | Eight people at the covered court need more blankets and sleeping mats; nights are cold. | Eight people in the evacuation center have insufficient blankets and feel cold at night. | not scored |

### 5. no-need

> Update lang po: nakarating na kami ng pamilya ko dito sa evacuation center, nasa Room 2 kami. Okay naman kami lahat, salamat po.

- Expected priority: low (Low: no need stated)
- Actual priority: low (Low: no need stated)
- llm_ms: 17,162

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Room 2, evacuation center | Room 2, evacuation center | match |
| in_center | true | true | match |
| on_behalf | false | false | match |
| headcount | null | null | match |
| vulnerable | [] | [] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | [] | [] | match |
| summary | Reporter's family arrived safely at the evacuation center and is in Room 2; all are okay. | Family has arrived at Room 2 of the evacuation center and is all okay. | not scored |

## `gemma4:e2b`, run 2026-10-09T10:00:10-07:00

### Environment

- Ollama: http://172.23.48.1:11434, version 0.40.2 (from `/api/version`)
- Model: `gemma4:e2b`, digest `b37049369adf`, 4,587,156,505 bytes, 4.6B, Q4_K_M (from `/api/tags`)
- CPU: AMD Ryzen 5 2400G with Radeon Vega Graphics
- Ollama host OS: Microsoft Windows 11 Pro 10.0.26200
- Bench OS: Ubuntu 26.04.1 LTS, kernel 6.18.33.2-microsoft-standard-WSL2; 8 logical CPUs visible
- Loaded before unload (`/api/ps`): qwen3:4b; after unload: none
- Windows memory before warm-up: total 14,258 MiB, free 5,424 MiB
- Bench machine memory before warm-up: total 6,894 MiB, available 3,291 MiB
- Cold load (one warm-up request): 24,010 ms (24.0 s)
- Loaded after run (`/api/ps`): `gemma4:e2b`, size 970,803,117 bytes, size_vram 0, context 4096
- Windows memory after run: total 14,258 MiB, free 1,514 MiB

### Summary

| # | Report | Expected priority | Actual priority | Scored fields matched | llm_ms |
|---|---|---|---|---|---|
| 1 | outside-rescue-for-others | critical | critical | 8/8 | 33,853 |
| 2 | injury-in-center | critical | critical | 5/8 | 9,404 |
| 3 | vulnerable-supply-need | high | high | 6/8 | 9,745 |
| 4 | other-need | medium | medium | 7/8 | 7,709 |
| 5 | no-need | low | low | 8/8 | 7,482 |

Priority matched: 5/5. Scored fields matched: 34/40. Failed extractions: 0.
Successful `llm_ms`: min 7,482, median 9,404, max 33,853, total 68,193.

### 1. outside-rescue-for-others

> Paki-rescue po yung pamilya sa tapat ng bahay namin sa Sitio Malinis, Brgy. Bagong Silang. Apat sila nasa second floor, may buntis. Hanggang dibdib na ang baha.

- Expected priority: critical (Critical: rescue needed outside center)
- Actual priority: critical (Critical: rescue needed outside center)
- llm_ms: 33,853

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Sitio Malinis, Brgy. Bagong Silang | Sitio Malinis, Brgy. Bagong Silang | match |
| in_center | false | false | match |
| on_behalf | true | true | match |
| headcount | 4 | 4 | match |
| vulnerable | ["pregnant"] | ["pregnant"] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | ["rescue"] | ["rescue"] | match |
| summary | A family of four including a pregnant woman is trapped on a second floor by chest-high flood. | Four people including a pregnant woman are on the second floor in Sitio Malinis due to flooding. | not scored |

### 2. injury-in-center

> Nandito kami sa Gym ng evacuation center. Dalawa kaming nahiwa sa yero habang naglilinis, dumudugo pa yung kamay ko.

- Expected priority: critical (Critical: injury reported)
- Actual priority: critical (Critical: medical need)
- llm_ms: 9,404

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Gym, evacuation center | Gym ng evacuation center | no |
| in_center | true | true | match |
| on_behalf | false | false | match |
| headcount | 2 | null | no |
| vulnerable | [] | [] | match |
| injured | true | true | match |
| injury_note | cuts from metal sheet, hand still bleeding | two cuts from razor, hand bleeding | match |
| needs | [] | ["medical"] | no |
| summary | Two people in the gym were cut by metal sheeting while cleaning; one hand is still bleeding. | Two people were cut by a razor while cleaning and one hand is bleeding. | not scored |

### 3. vulnerable-supply-need

> Sa Room 7 po ng evacuation center, tatlo kami dito. Yung lolo namin 80 years old, ubos na yung maintenance na gamot niya para sa high blood.

- Expected priority: high (High: senior + medicine)
- Actual priority: high (High: senior + medicine)
- llm_ms: 9,745

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Room 7, evacuation center | Room 7 ng evacuation center | no |
| in_center | true | true | match |
| on_behalf | true | false | no |
| headcount | 3 | 3 | match |
| vulnerable | ["senior"] | ["senior"] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | ["medicine"] | ["medicine"] | match |
| summary | Family of three in Room 7; their 80-year-old grandfather has run out of blood pressure medicine. | Three people in Room 7 include an 80-year-old grandfather who has run out of blood pressure medicine. | not scored |

### 4. other-need

> Taga-covered court po kami sa evacuation center, walo kami dito. Kulang na po ang kumot at banig, malamig na sa gabi.

- Expected priority: medium (Medium: needs other)
- Actual priority: medium (Medium: needs other)
- llm_ms: 7,709

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | covered court, evacuation center | covered court | partial |
| in_center | true | true | match |
| on_behalf | false | false | match |
| headcount | 8 | 8 | match |
| vulnerable | [] | [] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | ["other"] | ["other"] | match |
| summary | Eight people at the covered court need more blankets and sleeping mats; nights are cold. | Eight people are at the covered court needing blankets and mats because it is cold at night. | not scored |

### 5. no-need

> Update lang po: nakarating na kami ng pamilya ko dito sa evacuation center, nasa Room 2 kami. Okay naman kami lahat, salamat po.

- Expected priority: low (Low: no need stated)
- Actual priority: low (Low: no need stated)
- llm_ms: 7,482

| Field | Expected | Actual | Match |
|---|---|---|---|
| location | Room 2, evacuation center | Room 2, evacuation center | match |
| in_center | true | true | match |
| on_behalf | false | false | match |
| headcount | null | null | match |
| vulnerable | [] | [] | match |
| injured | false | false | match |
| injury_note | null | null | match |
| needs | [] | [] | match |
| summary | Reporter's family arrived safely at the evacuation center and is in Room 2; all are okay. | The reporter's family has arrived at the evacuation center in Room 2 and is okay. | not scored |
