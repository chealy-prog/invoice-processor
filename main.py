import anthropic
import base64
import httpx
import ftplib
import io
import psycopg2
from flask import Flask, request, jsonify
from datetime import datetime
from pdf2image import convert_from_bytes
from PIL import Image

app = Flask(__name__)

FTP_HOST = 'connect.restaurant365.net'
FTP_USER = 'housepitality'
FTP_PASS = 'H@usePR365!'
FTP_DIR = '/housepitality/APImports/R365'
DATABASE_URL = 'postgresql://postgres:GkGZfSbGRykvjAPVNVxtCEZFldAFuwUa@postgres.railway.internal:5432/railway'

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
3 vs 8: 3 is open on the left. 8 is closed. Even a slightly malformed 8 will have TWO loops or bumps. If you see any hint of a second loop, it is 8 not 3.
3 vs B: In numeric fields, always use 3, never B.
4 vs 9: 4 has an open top. 9 is closed at top. Look for the closed loop.
5 vs 6: 5 has a flat top. 6 has a curved top that closes into a loop.
5 vs S: In numeric fields, always use 5, never S.
6 vs 8: 6 has one loop at bottom. 8 has two loops. Count the loops.
6 vs G: In numeric fields, always use 6, never G.
7 vs 2: 7 is angular with no curve at bottom. 2 curves at bottom.
8 vs 0: A broken or gapped oval with two sections = 8. Look for the pinch or crossing in the middle.
8 vs 3: 8 is CLOSED on both sides. 3 is OPEN on the left. If both sides are closed = 8.
8 vs B: In numeric fields, always use 8, never B.
9 vs 4: 9 is closed at top like a loop. 4 is open at top.
9 vs g/q: In numeric fields, always use 9, never g or q.
"""

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS items (
            product_code VARCHAR(20) PRIMARY KEY,
            product_name VARCHAR(255),
            unit_price DECIMAL(10,2),
            uom VARCHAR(20),
            seen_count INTEGER DEFAULT 1,
            last_seen TIMESTAMP DEFAULT NOW()
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

def get_known_items():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT product_code, product_name, unit_price, uom, seen_count FROM items ORDER BY seen_count DESC')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row[0]: {'name': row[1], 'price': float(row[2]), 'uom': row[3], 'count': row[4]} for row in rows}
    except Exception as e:
        print(f"DB read error: {e}")
        return {}

def update_items_db(csv_text):
    try:
        conn = get_db()
        cur = conn.cursor()
        for line in csv_text.strip().split('\n'):
            if not line.strip() or line.startswith('Vendor,'):
                continue
            cols = line.split(',')
            if len(cols) < 13:
                continue
            product_code = cols[4].strip()
            product_name = cols[5].strip()
            try:
                unit_price = float(cols[8].strip())
            except ValueError:
                continue
            uom = cols[6].strip()
            cur.execute('''
                INSERT INTO items (product_code, product_name, unit_price, uom, seen_count, last_seen)
                VALUES (%s, %s, %s, %s, 1, NOW())
                ON CONFLICT (product_code) DO UPDATE SET
                    seen_count = items.seen_count + 1,
                    last_seen = NOW(),
                    product_name = EXCLUDED.product_name,
                    unit_price = EXCLUDED.unit_price,
                    uom = EXCLUDED.uom
            ''', (product_code, product_name, unit_price, uom))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB write error: {e}")

def build_reference_list(known_items):
    if not known_items:
        return ""
    lines = ["KNOWN ITEM REFERENCE DATABASE (from previously processed invoices):"]
    lines.append("If you see a product code that matches one below, use the known name and price as a reference.")
    lines.append("If your reading differs from the reference, double-check your reading carefully.\n")
    for code, item in list(known_items.items())[:50]:
        confidence = "HIGH" if item['count'] >= 5 else "MEDIUM" if item['count'] >= 2 else "LOW"
        lines.append(f"  {code} | {item['name']} | ${item['price']:.2f} | {item['uom']} | Confidence: {confidence} (seen {item['count']}x)")
    return '\n'.join(lines)

def img_to_b64(img, quality=95):
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode('utf-8')

def pdf_to_images_with_crops(pdf_bytes):
    Image.MAX_IMAGE_PIXELS = None
    images = convert_from_bytes(pdf_bytes, dpi=200)
    num_pages = len(images)
    result = []

    for page_num, img in enumerate(images):
        # Resize multi-page docs but keep single page receipts full res
        if num_pages > 1:
            max_dimension = 2000
            if img.width > max_dimension or img.height > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

        w, h = img.size

        # Full page image
        full_b64 = img_to_b64(img, quality=90)
        result.append({
            'type': 'full',
            'page': page_num + 1,
            'b64': full_b64
        })

        # Crop product code column (leftmost ~15% of page, skip top 10% header)
        code_crop = img.crop((
            int(w * 0.00),  # left edge
            int(h * 0.10),  # skip header
            int(w * 0.15),  # right edge of code column
            int(h * 0.95)   # bottom
        ))
        # Scale up 2x for clarity
        code_crop = code_crop.resize((code_crop.width * 2, code_crop.height * 2), Image.LANCZOS)
        result.append({
            'type': 'codes',
            'page': page_num + 1,
            'b64': img_to_b64(code_crop, quality=98)
        })

        # Crop qty + unit price + total columns (rightmost ~35% of page)
        numbers_crop = img.crop((
            int(w * 0.65),  # left edge of numbers area
            int(h * 0.10),  # skip header
            int(w * 1.00),  # right edge
            int(h * 0.95)   # bottom
        ))
        # Scale up 2x for clarity
        numbers_crop = numbers_crop.resize((numbers_crop.width * 2, numbers_crop.height * 2), Image.LANCZOS)
        result.append({
            'type': 'numbers',
            'page': page_num + 1,
            'b64': img_to_b64(numbers_crop, quality=98)
        })

    return result

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

with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"DB init error: {e}")

@app.route('/process', methods=['POST'])
def process_invoice():
    data = request.json
    file_url = data.get('file_url')
    api_key = data.get('api_key')
    location_code = data.get('location_code', '')
    upload_date = datetime.now().strftime('%-m/%-d/%Y')

    file_response = httpx.get(file_url)
    file_bytes = file_response.content
    content_type = file_response.headers.get('content-type', '')

    known_items = get_known_items()
    reference_list = build_reference_list(known_items)

    is_pdf = 'pdf' in content_type or file_url.lower().endswith('.pdf')
    content_blocks = []

    if is_pdf:
        try:
            page_images = pdf_to_images_with_crops(file_bytes)

            # Send full pages first
            for item in page_images:
                if item['type'] == 'full':
                    content_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": item['b64']}
                    })

            # Then send cropped code columns with label
            content_blocks.append({
                "type": "text",
                "text": "The following are high-resolution crops of the PRODUCT CODE column only — use these to read product codes accurately:"
            })
            for item in page_images:
                if item['type'] == 'codes':
                    content_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": item['b64']}
                    })

            # Then send cropped numbers columns with label
            content_blocks.append({
                "type": "text",
                "text": "The following are high-resolution crops of the QTY, UNIT PRICE, and TOTAL columns — use these to read numbers accurately:"
            })
            for item in page_images:
                if item['type'] == 'numbers':
                    content_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": item['b64']}
                    })

        except Exception as e:
            print(f"Image conversion failed, falling back to PDF beta: {e}")
            file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            content_blocks = [{
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": file_base64}
            }]
    else:
        file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
        content_blocks = [{
            "type": "image",
            "source": {"type": "base64", "media_type": content_type or "image/jpeg", "data": file_base64}
        }]

    content_blocks.append({
        "type": "text",
        "text": f"""This document is a photo or scan of a printed invoice or receipt from Virginia ABC. You have been provided:
1. The full page image(s) for context
2. High-resolution crops of the product code column
3. High-resolution crops of the qty/price/total columns

Use the high-resolution crops to read numbers and codes accurately. Use the full page for context and row alignment.

{DIGIT_AMBIGUITY_GUIDE}

{reference_list}

When reading any number, consult the ambiguity guide above. If a digit looks unusual, use the guide to resolve it. If a product code matches one in the reference database, use that as a strong hint. Always verify using math: Qty × Unit Price = Total.

YOU MUST FOLLOW THESE STEPS IN ORDER. DO NOT SKIP ANY STEP.

STEP 1 — READ PRODUCT CODES:
Use the high-resolution product code crop. Read every code top to bottom. Use the digit ambiguity guide. Cross-reference with the known items database.

STEP 2 — READ PRODUCT NAMES:
Use the full page image. Read every product name top to bottom.

STEP 3 — READ ORDER QTY:
Use the high-resolution numbers crop. Read every quantity top to bottom. These are often 2-digit numbers like 14, 24, 10.

STEP 4 — READ UNIT PRICE:
Use the high-resolution numbers crop. Read every unit price top to bottom. Cross-reference with the known items database.

STEP 5 — READ TOTAL AMOUNT:
Use the high-resolution numbers crop. Read every total top to bottom.

STEP 6 — READ DATE AND GRAND TOTAL:
Find the date and grand total. If no date is present, use today's date: {upload_date}.

STEP 7 — COMBINE INTO ROWS:
Match each product code with its name, qty, unit price, and total by position.
For every row verify: Qty × Unit Price = Total.
If they don't match, use the digit ambiguity guide to re-read ambiguous digits.
Use Total ÷ Unit Price to calculate correct Qty if needed.

STEP 8 — VERIFY GRAND TOTAL:
Sum all Total values. Confirm they match the grand total.

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
    })

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": content_blocks
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

    update_items_db(fixed_csv)

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
        "flagged": flagged,
        "known_items_in_db": len(known_items)
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
