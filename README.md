# Pharmalink

## Inventory and prescription flows

- Purchase-order forecasts are built from completed `sales_items` history by medicine. Prophet is fitted by the background refresh; forecast demand, unexpired stock, minimum/maximum stock, pack size, and the order cap determine the displayed PO quantity. Automatic POs only use a completed Prophet forecast. When a medicine lacks enough history or Prophet is unavailable, the planner labels its recent-sales estimate and automatic Prophet restocking skips it rather than inventing a forecast.
- Customer prescriptions can be uploaded or captured by camera. Tesseract is tried first (configure `TESSERACT_PATH` if it is not on `PATH`); RapidOCR is the fallback. OCR text is matched against active medicines, and the screen shows current sellable stock, the earliest-expiring available lot's price, and any dosage/quantity recognized on the same OCR line. OCR values are suggestions only; customers should review the prescription and quantity before adding items to cart.
- The pharmacist inspects the original physical prescription at the store before dispensing. The cashier order workflow no longer requires an online prescription approval record.

## PWD ID verification

The POS accepts a scanned or pasted PWD ID QR payload, but applies the PWD discount only after the server-side verification provider authenticates it. The server issues a short-lived, signed session proof bound to the cashier and QR fingerprint; the sale endpoint validates and consumes that proof, rather than trusting browser state or requiring the provider to be reachable again after an e-wallet payment. The raw QR payload is not stored.

UPWDIS verification is not active until authorized integration access and its official API/portal documentation are supplied. No verification URL, request format, QR contents, or credentials are assumed. With no provider configured, verification returns unavailable and the PWD discount remains unapplied. `app/pwd_verification.py` defines the provider contract; `create_pwd_verification_provider()` is the registration point for an authorized implementation. Provider errors must be converted to `PwdVerificationUnavailableError` or `PwdVerificationProviderError`, and the adapter must avoid logging QR payloads or personal data.

For the official NCDA source, see [ncda.gov.ph](https://ncda.gov.ph/). Its publicly accessible pages did not provide an UPWDIS API contract or QR verification integration instructions during implementation.

The Hostinger setup and deploy workflow install the Tesseract OCR system package. Other deployments need the Tesseract executable on `PATH` or an explicit `TESSERACT_PATH`; the Python `rapidocr-onnxruntime` dependency remains the fallback OCR engine.