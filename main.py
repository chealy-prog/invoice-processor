import anthropic
import base64
import httpx
import ftplib
import io
from flask import Flask, request, jsonify

app = Flask(__name__)

FTP_HOST = 'connect.restaurant365.net'
FTP_USER = 'housepitality'
FTP_PASS = 'H@usePR365!'
FTP_DIR = '/housepitality/APImports/R365'

COMMON_MISREADS = {
    '0': ['8', 'O'],
    '1': ['7', 'l', 'I'],
    '2': ['7', 'Z'],
    '3': ['8'],
    '5': ['6', 'S'],
    '6': ['8', '5', 'G'],
    '7': ['1', '2'],
    '8': ['0', '3', '6', 'B'],
    '9': ['4', 'q'],
    '4': ['9'],
}

def could_be_misread(read_value, calculated_value):
    read_str = f"{read_value:.2f}"
    calc_str = f"{calculated_value:.2f}"
    if len(read_str) != len(calc_str):
        return False
    differences = [(r, c) for r, c in zip(read_str, calc_str) if r != c]
    if len(differences) == 0:
        return True
    if len(differences) > 2:
        return False
    for read_char, calc_char in differences:
        if read_char in COMMON_MISREADS and calc_char in COMMON_MISREADS.get(read_char, []):
            continue
        if calc_char in COMMON_MISREADS and read_char in COMMON_MISREADS.get(calc_char, []):
            continue
        return False
    return True

def validate_and_fix_csv(csv_text, invoice_total=None):
    lines = csv_text.strip().split('\n')
    fixed_lines = []
    flagged = []
    running_total = 0.0
    header = 'Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location'

    for line in lines:
        if not line.strip():
            continue
        if line.startswith('Vendor,'):
            fixed_lines.append(header)
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

    if invoice_total:
        try:
            claude_read_total = float(invoice_total)
            diff = abs(round(running_total, 2) - claude_read_total)
            if diff < 0.10:
                flagged.append(f"GRAND TOTAL VERIFIED: line items sum to ${running_total:.2f}")
            elif could_be_misread(claude_read_total, running_total):
                flagged.append(f"GRAND TOTAL OK: Claude read ${claude_read_total:.2f} from image but line items sum to ${running_total:.2f} — difference of ${diff:.2f} is consistent with a common misread. Trusting the math.")
            else:
                flagged.append(f"GRAND TOTAL MISMATCH: Claude read ${claude_read_total:.2f} from image but line items sum to ${running_total:.2f} — difference of ${diff:.2f} cannot be explained by a misread. Manual review required.")
        except ValueError:
            pass
    else:
        flagged.append(f"GRAND TOTAL (calculated): ${running_total:.2f}")

    return '\n'.join(fixed_lines), flagged, round(running_total, 2)

def upload_to_ftp(filename, content):
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, 21)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    ftp.cwd(FTP_DIR)
    ftp.storbinary(f'STOR {filename}', io.BytesIO(content.encode('utf-8')))
    ftp.quit()

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
                    "text": f"""This document is a photo or scan of a printed invoice table. Image quality may vary.

YOU MUST FOLLOW THESE STEPS IN ORDER. DO NOT SKIP ANY STEP.

STEP 1 — READ PRODUCT CODES COLUMN ONLY:
Look only at the leftmost column. Read every product code from top to bottom, one per line. List them all before doing anything else. There should be 40+ codes.

STEP 2 — READ PRODUCT NAMES COLUMN ONLY:
Look only at the product name column. Read every product name from top to bottom. List them all.

STEP 3 — READ ORDER QTY COLUMN ONLY:
Look only at the Order Qty column. Read every quantity from top to bottom. List them all. These are often 2-digit numbers like 14, 24, 10.

STEP 4 — READ UNIT PRICE COLUMN ONLY:
Look only at the Unit Price column. Read every unit price from top to bottom. List them all.

STEP 5 — READ TOTAL AMOUNT COLUMN ONLY:
Look only at the Total Amount column. Read every total from top to bottom. List them all.

STEP 6 — READ GRAND TOTAL:
Find the grand total at the bottom of the last page. Record it.

STEP 7 — COMBINE INTO ROWS:
Match each product code with its corresponding name, qty, unit price, and total by position (1st code matches 1st name matches 1st qty etc).
For every row verify: Qty × Unit Price = Total.
If they don't match, use Total ÷ Unit Price to get the correct Qty.

STEP 8 — VERIFY GRAND TOTAL:
Sum all Total values. Confirm they match the grand total from Step 6.
If they don't match, find and fix the discrepancy.

STEP 9 — OUTPUT CSV:
Return the data as a CSV with exactly these columns:

Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location

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

After the last CSV row add this line:
GRAND_TOTAL:[the grand total number only, no $ sign]

Return only the CSV rows and the GRAND_TOTAL line. No explanation. No markdown. No column lists from the intermediate steps."""
                }
            ]
        }]
    )

    raw_output = message.content[0].text

    invoice_total = None
    csv_lines = []
    for line in raw_output.strip().split('\n'):
        if line.startswith('GRAND_TOTAL:'):
            invoice_total = line.replace('GRAND_TOTAL:', '').strip()
        else:
            csv_lines.append(line)

    csv_text = '\n'.join(csv_lines)
    fixed_csv, flagged, calculated_total = validate_and_fix_csv(csv_text, invoice_total)

    try:
        first_data_line = [l for l in fixed_csv.split('\n') if l and not l.startswith('Vendor,')][0]
        cols = first_data_line.split(',')
        doc_number = cols[2].strip()
        date = cols[3].strip().replace('/', '')
        filename = f"Colin_Export_VABC_{doc_number}_{date}.csv"
    except Exception:
        filename = f"Colin_Export_VABC_invoice.csv"

    ftp_status = "success"
    try:
        upload_to_ftp(filename, fixed_csv)
    except Exception as e:
        ftp_status = f"FTP error: {str(e)}"
        flagged.append(ftp_status)

    return jsonify({
        "result": fixed_csv,
        "filename": filename,
        "ftp_status": ftp_status,
        "calculated_total": calculated_total,
        "claude_read_total": invoice_total,
        "flagged": flagged
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
