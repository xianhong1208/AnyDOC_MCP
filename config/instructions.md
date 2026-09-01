# AnyDoc — document format conversion service

Converts the user's uploaded files into the format they need, and provides PDF
splitting, page extraction, rotation and encryption.

## When to use this service

The user has uploaded a file and mentions converting, changing, saving,
exporting or downloading it as some format, or wants to read / summarize a
document they uploaded.

## Service boundary (read this first; it saves wasted calls)

Only these five categories are handled:

| Category | Formats |
|---|---|
| Documents | pdf, docx, doc, odt, rtf, epub |
| Spreadsheets | xlsx, xls, ods, csv, tsv |
| Presentations | pptx, ppt, odp |
| Text | md, html, txt, rst, tex, json, xml |
| Images | png, jpg, webp, gif, bmp, tiff |

**Never supported: audio (mp3 / wav / m4a), video (mp4 / mov), archives
(zip / rar), CAD, executables.** When the user asks for one of these,
**tell them directly that this service cannot do it** and do not call any tool;
no combination of parameters will make it succeed.

Treat a tool response of "unsupported" or "could not recognize the format" the
same way: relay it to the user and do not retry with different parameters.
Repeated calls only slow the response down; the outcome will not change.

## Choosing a tool

| User intent | Tool to use |
|---|---|
| "What is this PDF about", "summarize this document" | `extract_text` — returns readable text |
| "Convert to PDF", "make a Word file", "save as JPG" | `convert_document` — returns a downloadable file |
| "What is this file", "how many pages" | `inspect_document` |
| "Which formats are supported", "can this be converted" | `list_supported_conversions` |
| "Only pages 3 to 5", "reorder the pages" | `pdf_extract_pages` |
| "One file per page", "split it up" | `pdf_split` |
| "These pages are sideways", "rotate them" | `pdf_rotate` |
| "Add a password", "protect this file" | `pdf_protect` |

**The most common mistake is confusing `extract_text` with `convert_document`.**
The rule is simple: the user wants **you to read** the document -> `extract_text`;
the user wants **to receive a file** -> `convert_document`.

## Key rules of behaviour

1. **Never ask the user for the source format**; the service detects it from
   the file automatically.
2. **Always relay the warning attached to a conversion result; that is the
   condition under which lossy conversion is allowed.** Paths such as PDF back
   to Word only preserve text and images; layout, fonts and table styling are
   rebuilt. The service **converts anyway** rather than blocking you, but when
   the response contains a ⚠️, proactively tell the user: "the content and
   images are there, but the layout needs re-adjusting, and the images are
   collected at the end of the document". Reporting only "conversion complete"
   leaves the user to discover the problem when they open the file.
3. **When a conversion fails, read the error message before deciding the next step**:
   - `NO_CONVERSION_PATH` — this path is not feasible. Use
     `list_supported_conversions` to find alternatives and propose them to the
     user (e.g. PDF cannot go straight to PowerPoint, but the text can be
     extracted and rebuilt).
   - `UNSUPPORTED_FORMAT` — usually the file name has no extension; ask the
     user for the complete file name.
   - `CONVERSION_FAILED` — the file itself may be corrupt or password-protected;
     ask the user to check.
4. **Check the page count before `pdf_split`.** A 200-page PDF split one page
   per file produces 200 files; look at the page count with `inspect_document`
   first and confirm the split with the user.
5. **The password stays in the conversation history.** When using
   `pdf_protect`, remind the user not to reuse a password from another system.
6. **Only one file can be processed per call.** If the user uploads several
   files and asks to merge or batch-convert them, handle them one at a time and
   say the work happens in several steps; there is currently no tool that
   merges several uploaded files.
7. **Always put the upload reference into `file_content`.**
   When you see a reference such as `[Uploaded Artifact: "report.pptx"]`, put it
   into `file_content`, not into `file_name` and not into `target_format`.
   For `file_name`, **do not send the parameter at all** (not even `null`); the
   host fills it in automatically. `target_format` holds only the target format
   (e.g. `"pdf"`).

8. **Always pass `output_name` to `convert_document`, set to the stem of the
   original file name.** When the user uploads
   `[Uploaded Artifact: "Vendor Review 2026.pptx"]`, `output_name` is
   `Vendor Review 2026` (no extension). Without it the user downloads a file
   called `converted.pdf` and cannot tell which document it is. If the user
   asked for a new name, use that instead.

## Quality expectations

- Office -> PDF: layout fully preserved; safe to use as is.
- Markdown / HTML -> Word / PDF: structure is correct; styling is the default template.
- PDF -> any editable format: **only text and images survive; the layout is rebuilt**.
  This is a limitation of the PDF format itself, not a defect of the service.
  Always let the user know before proceeding.

  In the output file, the PDF's embedded images are collected in an
  "Images from the original document" section at the end, labelled with the
  page they came from. Image coordinates in a PDF have no relation to the
  reading order of the text, so the original placement cannot be restored.
  **Tell the user this proactively**, so they know to move the images back to
  the right paragraphs instead of assuming the misplaced images are a
  conversion error.
