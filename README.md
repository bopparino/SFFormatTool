# Salesforce → ADP Payroll Converter

A small desktop app that converts a weekly Salesforce labor remittance
Excel report into the ADP-ready `PRJISEPI.csv` file. The user drops in
the `.xlsx` exported from Salesforce, clicks **Convert**, and gets a
single CSV ready to upload to ADP.

The app handles:

- Pay-period detection from the Salesforce header (used to derive the
  ADP Batch ID — the ISO week number of the pay period end date).
- Per-row classification: piecework when `Amount == Amount Owed`,
  hourly otherwise.
- Per-employee chronological overtime calculation (hours past 40 are
  OT; rows that straddle 40 are split).
- DE vs non-DE state bucketing — DE work goes on its own row.
- Output rows in ADP's mandated 25-column format with the file name
  forced to `PRJISEPI.csv`.

## Project layout

```
payroll-converter/
├── main.py                          GUI + entry point
├── converter.py                     pure parsing / aggregation logic
├── tests/test_converter.py          pytest tests (unit + end-to-end)
├── samples/sample_input.xlsx        reference Salesforce report
├── samples/sample_template.csv      reference ADP header row
├── requirements.txt
├── .github/workflows/build-windows.yml
└── README.md
```

## Local development (macOS)

```bash
cd payroll-converter
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

The app uses `customtkinter` for a modern dark-themed UI and
`tkinterdnd2` for drag-and-drop. On macOS, drag-and-drop into a
Tk window works but is less polished than on Windows; clicking the
drop zone to open a file picker always works.

## Tests

```bash
pytest -q
```

There are 38 tests covering classification, state extraction, file #
zero-padding, ISO week, OT chronological accumulation (under-40, over-40,
straddle-40, DE/non-DE preservation, chronological vs source order,
piecework excluded), aggregation buckets, row generation (1 row vs 2
rows for DE-mixed employees), and an end-to-end run against
`samples/sample_input.xlsx`.

## Building the Windows `.exe`

PyInstaller can't cross-compile from macOS, so the `.exe` is built on
GitHub Actions (`.github/workflows/build-windows.yml`).

1. Push to `main` (or run the workflow manually via the **Actions** tab
   → **Build Windows EXE** → **Run workflow**).
2. Wait for the run to finish.
3. Open the run, scroll to **Artifacts**, download
   `PayrollConverter-windows.zip`.
4. The zip contains `PayrollConverter.exe` — double-click on Windows
   to run.

### Manual Windows build (if you have access to a Windows box)

```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed ^
    --name PayrollConverter ^
    --collect-all customtkinter ^
    --collect-all tkinterdnd2 ^
    main.py
```

The resulting `dist\PayrollConverter.exe` is the single double-clickable
binary to ship.

## Configuration & error log

The app stores small bits of state (last-used input/output folder) in:

- Windows: `%APPDATA%\PayrollConverter\config.json`
- macOS:   `~/Library/Application Support/PayrollConverter/config.json`

If anything unexpected happens, the full Python traceback is written to
`error.log` in that same folder. The user-facing UI never shows a
traceback — only plain-English messages.

## Output format

The output is always named `PRJISEPI.csv` (this is enforced — the save
dialog defaults to it, and any deviation is coerced back). The CSV
header is fixed by ADP and includes intentional duplicate column names
(`Hours 3 Code`/`Hours 3 Amount` repeated four times, same for
`Hours 4`); these columns are always emitted blank.

Per-employee output:

| Situation                   | Rows                                     |
| --------------------------- | ---------------------------------------- |
| All work in non-DE states   | 1 row, non-DE values                     |
| All work in DE              | 1 row, DE values                         |
| Mixed DE + non-DE           | 2 rows: non-DE first, then DE            |

The DE row carries no special column flag — the payroll team flags DE
manually in ADP after upload. That's expected workflow, not a bug.

Rows are sorted by `File #` ascending; for mixed employees, non-DE
appears before DE.
