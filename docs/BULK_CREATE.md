# Bulk Create from Folder

## Overview

Bulk Create lets you import an entire folder of PDFs at once. The system scans recursively, classifies each PDF (application, quote, or supporting document), groups them by insured name, and creates submissions in batch.

## How to Use

1. Click **+ New Risk(s)** on the kanban board
2. In the modal, choose **Option 3: Bulk Create from Folder**
3. Click **Choose Folder** and select a folder containing PDFs
   - All subfolders are scanned recursively
   - Only `.pdf` files are processed (other file types are ignored)
4. Wait for classification (this may take a while for large folders)
5. Review the preview:
   - Each group shows the insured name, detected files, and target stage
   - Uncheck any groups you don't want to create
   - Unclassified files are shown separately and skipped
6. Click **Create All** to execute
7. Review results and click **Done**

## Where Cards End Up

The stage a card lands in depends on what documents are present for that insured:

| Documents Present | Target Stage |
|---|---|
| Application only | **Submission** (blue) |
| Application + Quote(s) | **Quoting** (orange) |
| Any combination with a Binder | **Bound** (green border) |

- Supporting documents (loss runs, SOVs, finance agreements) are attached to the submission but don't affect which stage it lands in.
- A binder always triggers bound stage regardless of other documents present.

## Document Classification

The system classifies PDFs using keyword signals in the extracted text:

- **Application** — ACORD forms (125, 126, 127, 130, etc.), submission forms
- **Quote** — Premium indications, proposals, coverage offers from carriers/underwriters
- **Supporting Documents:**
  - Loss Run — claims history reports
  - SOV (Statement of Values) — property schedules with replacement values
  - Binder — temporary evidence of coverage
  - Finance Agreement — premium financing terms and payment schedules

For scanned PDFs (no extractable text), the system uses AI vision to classify from the first page image.

## Insured Name Grouping

Files are grouped by insured name using fuzzy matching:
- "ABC Corp" and "ABC Corporation" → same group
- "Cutscape, LLC" and "CUTSCAPE, LLC" → same group  
- "LTR Holdings, LLC dba Wolf Disposals" and "LTR Holdings LLC dba Wolf Disposals" → same group

The insured name is extracted from the document text (or via AI for scanned docs).

## Folder Structure Tips

The system doesn't care about folder structure — it scans everything recursively. But organizing by insured helps you verify the preview:

```
my_submissions/
├── Acme Corp/
│   ├── ACORD_125_Application.pdf
│   ├── GL_Quote_Great_American.pdf
│   └── Loss_Run_5yr.pdf
├── Wolf Disposals/
│   ├── ACORD_127_Application.pdf
│   └── quote_wolf.pdf
└── Cutscape/
    ├── application.pdf
    └── quote.pdf
```

## Performance Notes

- Digital PDFs classify almost instantly (text extraction + keyword matching)
- Scanned PDFs require an AI vision call per file (~1-2 seconds each)
- The creation step parses each application and quote through the full AI pipeline
- For a folder with 10 PDFs: expect ~30-60 seconds total depending on mix of digital/scanned
