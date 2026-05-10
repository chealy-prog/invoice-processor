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
0 vs 8: 0 is a clean oval. 8 has two loops. A gap in an oval = still 8.
1 vs 7: 1 is straight. 7 has a horizontal top bar.
3 vs 8: 3 is OPEN on the left. 8 is CLOSED on both sides. Any hint of two bumps = 8.
4 vs 9: 4 has open top. 9 has closed loop at top.
5 vs 6: 5 has flat top. 6 has curved top closing into a loop.
6 vs 8: 6 has ONE loop. 8 has TWO loops. Count the loops.
In numeric fields never use letters: always 0 not O, 1 not l/I, 5 not S, 8 not B.
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
    lines = ["KNOWN ITEM REFERENCE DATABASE:"]
    lines.append("Use these as reference. If your reading differs, double-check carefully.\n")
    for code, item in list(known_items.items())[:50]:
        confidence = "HIGH" if item['count'] >= 5 else "MEDIUM" if item['count'] >= 2 else "LOW"
        lines.append(f"  {code} | {item['name']} | ${item['price']:.2f} | {item['uom']} | {confidence} (seen {item['count']}x)")
    return '\n'.join(lines)

def img_to_b64(img, quality=90):
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode('utf-8')

def prepare_images(pdf_bytes):
    Image.MAX_IMAGE_PIXELS = None
    images = convert_from_bytes(pdf_bytes, dpi=200)
    num_pages = len(images)
    result = {'pages': [], 'crops': {}}

    for page_num, img in enumerate(images):
        w, h = img.size

        if num_pages > 1:
            thumb = img.copy()
            thumb.thumbnail((1600, 1600), Image.LANCZOS)
            result['pages'].append(img_to_b64(thumb, quality=75))
        else:
            result['pages'].append(img_to_b64(img, quality=90))

        # Only crop on single page or first page
        if page_num == 0:
            top = int(h * 0.10)
            bottom = int(h * 0.95)

            # Product codes column (left ~15%)
            codes = img.crop((0, top, int(w * 0.15), bottom))
            codes = codes.resize((codes.width * 3, codes.height * 3), Image.LANCZOS)
            result['crops']['codes'] = img_to_b64(codes, quality=97)

            # Product names column (15-55%)
            names = img.crop((int(w * 0.15), top, int(w * 0.55), bottom))
            names = names.resize((names.width * 2, names.height * 2), Image.LANCZOS)
            result['crops']['names'] = img_to_b64(names, quality=90)

            # Qty column (~55-65%)
            qty = img.crop((int(w * 0.55), top, int(w * 0.65), bottom))
            qty = qty.resize((qty.width * 4, qty.height * 4), Image.LANCZOS)
            result['crops']['qty'] = img_to_b64(qty, quality=97)

            # Unit price column (~65-80%)
            price = img.crop((int(w * 0.65), top, int(w * 0.80), bottom))
            price = price.resize((price.width * 4, price.height * 4), Image.LANCZOS)
            result['crops']['price'] = img_to_b64(price, quality=97)

            # Total column (~80-100%)
            total = img.crop((int(w * 0.80), top, w, bottom))
            total = total.resize((total.width * 4, total.height * 4), Image.LANCZOS)
            result['crops']['total'] = img_to_b64(total, quality=97)

    return result

def claude_call(client, images, prompt):
    content = []
    for img in images:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}})
    content.append({"type": "text", "text": prompt})
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": content}]
    )
    return msg.content[0].text.strip()

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
                flagged.append(f"GRAND TOTAL VERIFIED: ${running_total:.2f}")
            elif could_be_misread(claude_read_total, running_total):
                flagged.append(f"GRAND TOTAL OK: Claude read ${claude_read_total:.2f}, calculated ${running_total:.2f} — consistent with common misread. Trusting math.")
            else:
                flagged.append(f"GRAND TOTAL MISMATCH: Claude read ${claude_read_total:.2f}, calculated ${running_total:.2f} — difference ${abs(running_total - claude_read_total):.2f}. Manual review required.")
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

    client = anthropic.Anthropic(api_key=api_key)

    is_pdf = 'pdf' in content_type or file_url.lower().endswith('.pdf')

    if is_pdf:
        try:
            imgs = prepare_images(file_bytes)
            pages = imgs['pages']
            crops = imgs['crops']

            # CALL 1: Get product codes from high-res crop
            codes_text = claude_call(client, [crops['codes']], f"""This is a high-resolution crop of the PRODUCT CODE column from a Virginia ABC invoice.
{DIGIT_AMBIGUITY_GUIDE}
{reference_list}
Read every product code from top to bottom. Return only a numbered list like:
1. 011297
2. 015626
...
No explanation. Numbers only.""")

            # CALL 2: Get product names from full page
            names_text = claude_call(client, pages, f"""This is a Virginia ABC invoice.
Read every product name from top to bottom in the same order as the product codes.
Return only a numbered list like:
1. crown royal whisky
2. jameson irish whiskey
...
No explanation. Names in lowercase only.""")

            # CALL 3: Get quantities from high-res crop
            qty_text = claude_call(client, [crops['qty']], f"""This is a high-resolution crop of the ORDER QTY column from a Virginia ABC invoice.
{DIGIT_AMBIGUITY_GUIDE}
Quantities are often 2 digits: 10, 14, 24 are common. Read carefully.
Return only a numbered list like:
1. 2
2. 1
...
No explanation. Numbers only.""")

            # CALL 4: Get unit prices and totals from high-res crops
            prices_text = claude_call(client, [crops['price'], crops['total']], f"""These are high-resolution crops of the UNIT PRICE column and TOTAL AMOUNT column from a Virginia ABC invoice.
{DIGIT_AMBIGUITY_GUIDE}
Return two numbered lists:
UNIT PRICES:
1. 38.99
2. 27.99
...
TOTALS:
1. 77.98
2. 27.99
...
Also include the grand total at the end like: GRAND_TOTAL:5763.74
No explanation. Numbers only.""")

            # CALL 5: Merge everything into CSV
            merge_prompt = f"""You are merging data from a Virginia ABC invoice that was read in separate passes.
Here is the data from each pass:

PRODUCT CODES:
{codes_text}

PRODUCT NAMES:
{names_text}

QUANTITIES:
{qty_text}

UNIT PRICES AND TOTALS:
{prices_text}

{reference_list}

Instructions:
- Match row 1 code with row 1 name with row 1 qty with row 1 price with row 1 total, etc.
- For every row verify: Qty × Unit Price = Total. If they don't match, use Total ÷ Unit Price to correct Qty.
- Find the document number and date from the data if mentioned, otherwise use today: {upload_date}

Return as CSV with these exact columns:
Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location

Column rules:
- Vendor: always VA ABC
- Location: always {location_code}
- Document Number: order/receipt number
- Date: M/D/YYYY format or {upload_date}
- Vendor Item Number: product code
- Vendor Item Name: product name in lowercase
- UofM: Bottle for 750ml, Liter for 1L, Each for anything else
- Qty: X.00 format
- Unit Price: no $ sign
- Total: no $ sign
- Image URL: {file_url}
- Break Flag: always N
- Detail Location: always {location_code}

After last row add: GRAND_TOTAL:[number only]
Return only CSV rows and GRAND_TOTAL. No explanation. No markdown."""

            final_text = claude_call(client, [], merge_prompt) if False else client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                messages=[{"role": "user", "content": [{"type": "text", "text": merge_prompt}]}]
            ).content[0].text.strip()

        except Exception as e:
            print(f"Multi-call failed: {e}, falling back to PDF beta")
            file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            final_text = client.beta.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                betas=["pdfs-2024-09-25"],
                messages=[{"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_base64}},
                    {"type": "text", "text": f"Extract all line items as CSV: Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location. Vendor=VA ABC, Location={location_code}, Image URL={file_url}, Break Flag=N, Detail Location={location_code}. Add GRAND_TOTAL at end."}
                ]}
            ).content[0].text.strip()
    else:
        file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
        final_text = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": content_type or "image/jpeg", "data": file_base64}},
                {"type": "text", "text": f"""Extract all line items from this Virginia ABC receipt as CSV.
{DIGIT_AMBIGUITY_GUIDE}
{reference_list}
Columns: Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location
Rules: Vendor=VA ABC, Location={location_code}, Date={upload_date} if not found, Image URL={file_url}, Break Flag=N, Detail Location={location_code}
Add GRAND_TOTAL:[number] at end. No header. No explanation."""}
            ]}
        ).content[0].text.strip()

    invoice_total = None
    csv_lines = []
    for line in final_text.strip().split('\n'):
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
