import anthropic
import base64
import httpx
import ftplib
import io
import json
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
 
def prepare_pages(pdf_bytes):
    Image.MAX_IMAGE_PIXELS = None
    images = convert_from_bytes(pdf_bytes, dpi=200)
    num_pages = len(images)
    pages = []
    for img in images:
        if num_pages > 1:
            thumb = img.copy()
            thumb.thumbnail((1600, 1600), Image.LANCZOS)
            pages.append((img_to_b64(thumb, quality=75), thumb.size))
        else:
            pages.append((img_to_b64(img, quality=90), img.size))
    return images, pages
 
def claude_call(client, images, prompt, max_tokens=2048):
    content = []
    for img in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img}
        })
    content.append({"type": "text", "text": prompt})
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}]
    )
    return msg.content[0].text.strip()
 
def claude_text_call(client, prompt, max_tokens=4096):
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}]
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
                flagged.append(f"GRAND TOTAL OK: Claude read ${claude_read_total:.2f}, calculated ${running_total:.2f} - consistent with common misread. Trusting math.")
            else:
                flagged.append(f"GRAND TOTAL MISMATCH: Claude read ${claude_read_total:.2f}, calculated ${running_total:.2f} - difference ${abs(running_total - claude_read_total):.2f}. Manual review required.")
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
            raw_images, pages = prepare_pages(file_bytes)
            page_b64s = [p[0] for p in pages]
 
            # CALL 1: Detect column boundaries
            detect_prompt = """This is a Virginia ABC invoice or receipt image.
I need to crop specific columns for high-resolution reading.
The image dimensions are: """ + str(raw_images[0].size[0]) + "x" + str(raw_images[0].size[1]) + """ pixels.
 
Please identify the pixel boundaries of these columns in the FIRST page:
- product_code: the column containing item/product codes (6-digit numbers)
- product_name: the column containing product names
- qty: the column containing order quantities
- unit_price: the column containing unit prices
- total: the column containing total amounts
- data_top: the y pixel where data rows start (after header)
- data_bottom: the y pixel where data rows end (before footer/totals)
 
Return ONLY a JSON object like this:
{
  "product_code": {"x1": 50, "x2": 150},
  "product_name": {"x1": 155, "x2": 400},
  "qty": {"x1": 405, "x2": 480},
  "unit_price": {"x1": 485, "x2": 580},
  "total": {"x1": 585, "x2": 700},
  "data_top": 120,
  "data_bottom": 900
}
 
Return only the JSON. No explanation."""
 
            bounds_text = claude_call(client, [page_b64s[0]], detect_prompt, max_tokens=500)
 
            # Parse bounds
            try:
                # Strip any markdown
                bounds_text = bounds_text.replace('```json', '').replace('```', '').strip()
                bounds = json.loads(bounds_text)
            except Exception as e:
                print(f"Bounds parse error: {e}, using fallback")
                bounds = {
                    "product_code": {"x1": 0, "x2": 150},
                    "product_name": {"x1": 150, "x2": 500},
                    "qty": {"x1": 500, "x2": 580},
                    "unit_price": {"x1": 580, "x2": 700},
                    "total": {"x1": 700, "x2": 850},
                    "data_top": 100,
                    "data_bottom": int(raw_images[0].size[1] * 0.92)
                }
 
            img = raw_images[0]
            w, h = img.size
            top = max(0, bounds.get('data_top', int(h * 0.10)))
            bottom = min(h, bounds.get('data_bottom', int(h * 0.95)))
 
            def make_crop(col_key, scale=3):
                col = bounds.get(col_key, {})
                x1 = max(0, col.get('x1', 0))
                x2 = min(w, col.get('x2', w))
                crop = img.crop((x1, top, x2, bottom))
                crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
                return img_to_b64(crop, quality=97)
 
            codes_b64 = make_crop('product_code', scale=4)
            names_b64 = make_crop('product_name', scale=2)
            qty_b64 = make_crop('qty', scale=4)
            price_b64 = make_crop('unit_price', scale=4)
            total_b64 = make_crop('total', scale=4)
 
            # CALL 2: Product codes
            codes_text = claude_call(
                client,
                [codes_b64],
                DIGIT_AMBIGUITY_GUIDE + "\n" + reference_list + """
 
This is a high-resolution crop of ONLY the product code column from a Virginia ABC invoice.
Read every product code from top to bottom.
Return only a numbered list:
1. 011297
2. 015626
No explanation. No extra text."""
            )
 
            # CALL 3: Product names + doc info
            names_text = claude_call(
                client,
                [page_b64s[0]],
                """This is a Virginia ABC invoice or receipt.
Read every product name from top to bottom.
Also find: document/order number, and date (if present).
Return:
DOCUMENT_NUMBER: [number]
DATE: [date or NONE]
NAMES:
1. crown royal whisky
2. jameson irish whiskey
No explanation."""
            )
 
            # CALL 4: Quantities
            qty_text = claude_call(
                client,
                [qty_b64],
                DIGIT_AMBIGUITY_GUIDE + """
 
This is a high-resolution crop of ONLY the order quantity column from a Virginia ABC invoice.
Quantities are often multi-digit: 10, 14, 24 are common. Read every digit carefully.
Return only a numbered list:
1. 2
2. 14
No explanation. Numbers only."""
            )
 
            # CALL 5: Prices and totals
            prices_text = claude_call(
                client,
                [price_b64, total_b64],
                DIGIT_AMBIGUITY_GUIDE + """
 
These are high-resolution crops of the UNIT PRICE column and TOTAL AMOUNT column from a Virginia ABC invoice.
Return:
UNIT PRICES:
1. 38.99
2. 31.99
TOTALS:
1. 77.98
2. 447.86
GRAND_TOTAL: 5763.74
No explanation. Numbers only."""
            )
 
            # Build merge prompt
            merge_prompt = "You are merging data from a Virginia ABC invoice read in separate passes.\n\n"
            merge_prompt += "PRODUCT CODES:\n" + codes_text + "\n\n"
            merge_prompt += "NAMES AND DOCUMENT INFO:\n" + names_text + "\n\n"
            merge_prompt += "QUANTITIES:\n" + qty_text + "\n\n"
            merge_prompt += "UNIT PRICES AND TOTALS:\n" + prices_text + "\n\n"
            merge_prompt += reference_list + "\n\n"
            merge_prompt += "Instructions:\n"
            merge_prompt += "- Match row 1 code with row 1 name with row 1 qty with row 1 price with row 1 total, etc.\n"
            merge_prompt += "- For every row verify: Qty x Unit Price = Total. If they do not match, use Total divided by Unit Price to correct Qty.\n"
            merge_prompt += "- Extract document number and date from NAMES AND DOCUMENT INFO above.\n"
            merge_prompt += "- If date is NONE, use: " + upload_date + "\n\n"
            merge_prompt += "Return as CSV with these exact columns:\n"
            merge_prompt += "Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location\n\n"
            merge_prompt += "Rules:\n"
            merge_prompt += "- Vendor: always VA ABC\n"
            merge_prompt += "- Location: always " + str(location_code) + "\n"
            merge_prompt += "- Document Number: from invoice\n"
            merge_prompt += "- Date: M/D/YYYY format\n"
            merge_prompt += "- Vendor Item Number: product code\n"
            merge_prompt += "- Vendor Item Name: product name in lowercase\n"
            merge_prompt += "- UofM: Bottle for 750ml, Liter for 1L, Each for anything else\n"
            merge_prompt += "- Qty: X.00 format\n"
            merge_prompt += "- Unit Price: no $ sign\n"
            merge_prompt += "- Total: no $ sign\n"
            merge_prompt += "- Image URL: " + file_url + "\n"
            merge_prompt += "- Break Flag: always N\n"
            merge_prompt += "- Detail Location: always " + str(location_code) + "\n\n"
            merge_prompt += "After last row add: GRAND_TOTAL:[number only]\n"
            merge_prompt += "Return only CSV rows and GRAND_TOTAL line. No header. No explanation. No markdown."
 
            final_text = claude_text_call(client, merge_prompt)
 
        except Exception as e:
            print(f"Multi-call failed: {e}, falling back to PDF beta")
            file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            msg = client.beta.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                betas=["pdfs-2024-09-25"],
                messages=[{"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_base64}},
                    {"type": "text", "text": "Extract all line items as CSV: Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location. Vendor=VA ABC, Location=" + str(location_code) + ", Image URL=" + file_url + ", Break Flag=N, Detail Location=" + str(location_code) + ", Date=" + upload_date + " if not found. Add GRAND_TOTAL at end. No markdown."}
                ]}]
            )
            final_text = msg.content[0].text.strip()
 
    else:
        file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
        receipt_prompt = DIGIT_AMBIGUITY_GUIDE + "\n" + reference_list + "\n"
        receipt_prompt += "Extract all line items from this Virginia ABC receipt as CSV.\n"
        receipt_prompt += "Columns: Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location\n"
        receipt_prompt += "Rules: Vendor=VA ABC, Location=" + str(location_code) + ", Date=" + upload_date + " if not found, Image URL=" + file_url + ", Break Flag=N, Detail Location=" + str(location_code) + "\n"
        receipt_prompt += "Add GRAND_TOTAL:[number] at end. No header. No explanation. No markdown."
 
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": content_type or "image/jpeg", "data": file_base64}},
                {"type": "text", "text": receipt_prompt}
            ]}]
        )
        final_text = msg.content[0].text.strip()
 
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
        filename = "Colin_Export_VABC_invoice.csv"
 
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
 
 
