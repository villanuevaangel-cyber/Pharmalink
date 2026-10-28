PharmaLink PWD popup - test samples (FAKE data, for testing only)

1_sample_PWD_ID_plain_qr.png      QR = plain PWD ID number 13-9999-000-0009999
2_sample_PWD_ID_json_qr.png       QR = JSON with PWD ID number and name
3_sample_PhilSys_National_ID.png  PhilSys-style QR + text -> "not a PWD ID"
4_sample_random_QR.png            random QR -> "No valid PWD ID number found"
5_sample_TIN_ID_no_QR.png         TIN ID, no QR (read from the card text) -> "This is a TIN ID"

Ways to test (all show the validation inside the PWD popup):
  - Scan ID (camera): QR is read first; if no QR can be read, the card text is read automatically.
  - Upload ID photo: pick a photo of the ID (works for IDs without a QR).
  - Type in the PWD ID Number field:
      13-9999-000-0009999   -> valid format
      1399990000009999      -> valid format (dashes added automatically)
      1234-5678-9012-3456   -> rejected: PhilSys National ID
      123-456-789-000       -> rejected: TIN
      13-9999               -> rejected: no valid PWD ID number

Reading card text needs OCR (Tesseract or RapidOCR, same as the prescription feature).
These numbers are not real, so the DOH PWD Registry will say "not found".
For the test, still tick the confirmation box to see the discount apply.
