import anthropic
import base64
import httpx
import ftplib
import io
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

FTP_HOST = 'connect.restaurant365.net'
FTP_USER = 'housepitality'
FTP_PASS = 'H@usePR365!'
FTP_DIR = '/housepitality/APImports/R365'

COMMON_MISREADS = {
    '0': ['8', 'O', 'D'],
    '1': ['7', 'l', 'I', 'i'],
    '2': ['7', 'Z'],
    '3': ['8', 'B'],
    '4': ['9', 'A'],
    '5': ['6', 'S', '$'],
    '6': ['8', '5', 'G', 'b'],
    '7': ['1', '2', 'T'],
    '8': ['0', '3', '6', 'B', 'S'],
    '9': ['4', 'q', 'g'],
}

DIGIT_AMBIGUITY_GUIDE = """
DIGIT AMBIGUITY REFERENCE LIST:
When a digit is unclear, use context (math, surrounding digits) to resolve it.
Common confusions in scanned/photographed/thermal printed documents:

0 vs 8: 0 is a clean oval. 8 has two loops. A gap or break in an oval = still 8, not 0.
0 vs O: Always use 0 in numeric fields, never the letter O.
1 vs 7: 1 is straight vertical. 7 has a horizontal top bar. If there's a serif or top bar = 7.
1 vs l/I: In numeric fields, always use 1, never lowercase l or uppercase I.
2 vs 7: 2 curves at the bottom. 7 is angular. Look for the curve.
3 vs 8: 3 is open on the left. 8 is closed. A slightly open or malformed 8 = still 8.
3 vs B: In numeric fields, always use 3, never B.
4 vs 9: 4 has an open top. 9 is closed at top. Look for the closed loop.
5 vs 6: 5 has a flat top. 6 has a curved top that closes into a loop.
5 vs S: In numeric fields, always use 5, never S.
6 vs 8: 6 has one loop at bottom. 8 has two loops. Count the loops.
6 vs G: In numeric fields, always use 6, never G.
7 vs 2: 7 is angular with no curve at bottom. 2 curves at bottom.
8 vs 0: See 0 vs 8 above. A broken or gapped oval with two sections = 8.
8 vs B: In numeric fields, always use 8, never B.
9 vs 4: 9 is closed at top like a loop. 4 is open at top.
9 vs g/q: In numeric fields, always use 9, never g or q.
"""

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
    upload_date = datetime.now().strftime('%-m/%-d/%Y')

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
                    "text": f"""This document is a photo or scan of a printed invoice or receipt from Virginia ABC. Image quality may vary — it could be a multi-page order invoice or a single-page thermal printer receipt. Handle both formats.

{DIGIT_AMBIGUITY_GUIDE}

When reading any number, consult the ambiguity guide above. If a digit looks unusual, use the guide to resolve it before recording the value. Always verify your reading using the math: Qty × Unit Price = Total.

YOU MUST FOLLOW THESE STEPS IN ORDER. DO NOT SKIP ANY STEP.

STEP 1 — READ PRODUCT CODES COLUMN ONLY:
Look only at the product code / Item / GTIN column. Read every product code from top to bottom. Use the digit ambiguity guide to resolve any unclear digits. List them all.

STEP 2 — READ PRODUCT NAMES COLUMN ONLY:
Look only at the product name column. Read every product name from top to bottom. List them all.

STEP 3 — READ ORDER QTY COLUMN ONLY:
Look only at the Order Qty column. Read every quantity from top to bottom. Use the digit ambiguity guide. These are often 2-digit numbers like 14, 24, 10. List them all.

STEP 4 — READ UNIT PRICE COLUMN ONLY:
Look only at the Unit Price column. Read every unit price from top to bottom. Use the digit ambiguity guide. List them all.

STEP 5 — READ TOTAL AMOUNT COLUMN ONLY:
Look only at the Total Amount column. Read every total from top to bottom. Use the digit ambiguity guide. List them all.

STEP 6 — READ DATE AND GRAND TOTAL:
Find the date and grand total. If no date is present on the document, use today's date: {upload_date}.
Record the grand total from the document.

STEP 7 — COMBINE INTO ROWS:
Match each product code with its corresponding name, qty, unit price, and total by position.
For every row verify: Qty × Unit Price = Total.
If they don't match, consult the digit ambiguity guide and re-read the ambiguous digits.
Use Total ÷ Unit Price to calculate correct Qty if needed.

STEP 8 — VERIFY GRAND TOTAL:
Sum all Total values. Confirm they match the grand total.
If they don't match, use the digit ambiguity guide to find and fix the misread digit.

STEP 9 — OUTPUT CSV:
Return the data as a CSV with exactly these columns:

Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location

Column rules:
- Vendor: always VA ABC
- Location: always {location_code}
- Document Number: order number or receipt number from the document
- Date: date from the document in M/D/YYYY format, or {upload_date} if no date found
- Vendor Item Number: product code or GTIN number
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
