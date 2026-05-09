import anthropic
import base64
import httpx
from flask import Flask, request, jsonify

app = Flask(__name__)

def validate_and_fix_csv(csv_text, invoice_total=None):
    lines = csv_text.strip().split('\n')
    fixed_lines = []
    flagged = []
    running_total = 0.0

    for line in lines:
        if not line.strip():
            continue
        cols = line.split(',')
        if len(cols) < 13:
            flagged.append(f"SHORT ROW: {line}")
            fixed_lines.append(line)
            continue

        try:
            qty = float(cols[7])
            unit_price = float(cols[8])
            total = float(cols[9])
            expected = round(qty * unit_price, 2)
            running_total += total

            if abs(expected - total) > 0.02:
                correct_qty = round(total / unit_price, 2)
                flagged.append(f"MATH ERROR fixed: {cols[5]} qty {qty} -> {correct_qty} (expected {expected} got {total})")
                cols[7] = f"{correct_qty:.2f}"
                line = ','.join(cols)
        except (ValueError, ZeroDivisionError):
            flagged.append(f"PARSE ERROR: {line}")

        fixed_lines.append(line)

    # Check against invoice grand total
    if invoice_total:
        try:
            expected_total = float(invoice_total)
            diff = abs(round(running_total, 2) - expected_total)
            if diff > 0.10:
                flagged.append(f"GRAND TOTAL MISMATCH: extracted ${running_total:.2f} vs invoice ${expected_total:.2f} — difference of ${diff:.2f}. Review all line items.")
            else:
                flagged.append(f"GRAND TOTAL VERIFIED: ${running_total:.2f} matches invoice ${expected_total:.2f}")
        except ValueError:
            pass

    return '\n'.join(fixed_lines), flagged

@app.route('/process', methods=['POST'])
def process_invoice():
    data = request.json
    file_url = data.get('file_url')
    api_key = data.get('api_key')
    location_code = data.get('location_code', '')

    file_response = httpx.get(file_url)
    file_base64 = base64.standard_b64encode(file_response.content).decode('utf-8')

    client = anthropic.Anthropic(api_key=api_key)
    message = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        betas=["pdfs-2024-09-25"],
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": file_base64
                    }
                },
                {
                    "type": "text",
                    "text": f"""This document is a photo or scan of a printed invoice. The image quality may vary — it could be perfectly flat, slightly angled, shadowy, or low resolution.

STEP 1 — EXTRACT EACH COLUMN VERTICALLY:
Before building any rows, read each column independently from top to bottom:
- Read ALL product codes top to bottom
- Read ALL product names top to bottom
- Read ALL sizes top to bottom
- Read ALL quantities top to bottom
- Read ALL unit prices top to bottom
- Read ALL totals top to bottom

This prevents misalignment between columns caused by image distortion.

STEP 2 — BUILD ROWS:
Combine the columns into rows. For each row verify: Qty × Unit Price = Total.
If they don't match, re-read that specific row from the image and correct it.
Use Total ÷ Unit Price to calculate correct Qty if needed.

STEP 3 — VERIFY GRAND TOTAL:
Find the grand total at the bottom of the invoice.
Sum all your line item totals and confirm they match the grand total.
If they don't match, find and fix the discrepancy before returning.

STEP 4 — RETURN RESULTS:
Return the data as a CSV with exactly these columns in this order:

Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location

Then on the very last line return the grand total like this:
GRAND_TOTAL:1234.56

Column rules:
- Vendor: always VA ABC
- Location: always {location_code}
- Document Number: order number from the invoice
- Date: pickup date in M/D/YYYY format
- Vendor Item Number: product code
- Vendor Item Name: product name in lowercase
- UofM: Bottle for 750ml, Liter for 1L, Each for anything else
- Qty: formatted as X.00
- Unit Price: no $ sign
- Total: no $ sign
- Image URL: {file_url}
- Break Flag: always N
- Detail Location: always {location_code}

Return only the CSV rows plus the GRAND_TOTAL line. No header. No explanation. No markdown."""
                }
            ]
        }]
    )

    raw_output = message.content[0].text

    # Extract grand total if present
    invoice_total = None
    csv_lines = []
    for line in raw_output.strip().split('\n'):
        if line.startswith('GRAND_TOTAL:'):
            invoice_total = line.replace('GRAND_TOTAL:', '').strip()
        else:
            csv_lines.append(line)

    csv_text = '\n'.join(csv_lines)
    fixed_csv, flagged = validate_and_fix_csv(csv_text, invoice_total)

    return jsonify({
        "result": fixed_csv,
        "flagged": flagged,
        "invoice_total": invoice_total
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
