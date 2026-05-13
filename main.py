import anthropic
import base64
import httpx
import ftplib
import io
import json
import cv2
import numpy as np
import pytesseract
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
from datetime import datetime
from pdf2image import convert_from_bytes
from PIL import Image

app = Flask(__name__)

FTP_HOST = 'connect.restaurant365.net'
FTP_USER = 'housepitality'
FTP_PASS = 'H@usePR365!'
FTP_DIR = '/housepitality/APImports/R365'
R365_URL = 'https://housepitality.restaurant365.com'
R365_USER = 'housepitalityAPI'
R365_PASS = 'pu5VJcpESkLA4Y'

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

# ── Image preprocessing ──────────────────────────────────────────────────────

def detect_rotation_osd(gray):
    try:
        img_pil = Image.fromarray(gray)
        osd = pytesseract.image_to_osd(img_pil, output_type=pytesseract.Output.DICT)
        angle = osd.get('rotate', 0)
        confidence = osd.get('orientation_conf', 0)
        print(f"OSD: rotate={angle}, confidence={confidence:.2f}")
        return angle, confidence
    except Exception as e:
        print(f"OSD failed: {e}")
        return 0, 0

def auto_rotate(gray):
    h, w = gray.shape
    if w > h * 1.2:
        print("Landscape -> rotating 90 CW")
        gray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
    angle, confidence = detect_rotation_osd(gray)
    if confidence > 1.0 and angle != 0:
        print(f"OSD correction: {angle} degrees")
        if angle == 90:
            gray = cv2.rotate(gray, cv2.ROTATE_90_COUNTERCLOCKWISE)
        elif angle == 180:
            gray = cv2.rotate(gray, cv2.ROTATE_180)
        elif angle == 270:
            gray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
    return gray

def deskew_fine(gray):
    try:
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=100,
                                minLineLength=gray.shape[1]*0.25, maxLineGap=20)
        if lines is None:
            return gray
        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if x2 == x1:
                continue
            angle = np.degrees(np.arctan2(y2-y1, x2-x1))
            if -15 < angle < 15:
                angles.append(angle)
        if not angles:
            return gray
        median_angle = np.median(angles)
        if abs(median_angle) < 0.3:
            return gray
        print(f"Fine deskew: {median_angle:.2f} degrees")
        h, w = gray.shape
        M = cv2.getRotationMatrix2D((w//2, h//2), median_angle, 1.0)
        return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_REPLICATE)
    except Exception as e:
        print(f"Deskew error: {e}")
        return gray

def preprocess_image(img_pil):
    """Full preprocessing pipeline: rotate, deskew, enhance, binarize."""
    img = np.array(img_pil.convert('RGB'))
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    print(f"Original: {gray.shape[1]}x{gray.shape[0]}")
    gray = auto_rotate(gray)
    gray = deskew_fine(gray)
    print(f"After correction: {gray.shape[1]}x{gray.shape[0]}")
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.fastNlMeansDenoising(gray, h=8)
    binary = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        25, 8
    )
    result = Image.fromarray(binary)
    result.thumbnail((2500, 2500), Image.LANCZOS)
    print(f"Final: {result.size}")
    return result

def img_to_b64(img, quality=90):
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode('utf-8')

def prepare_pages(pdf_bytes):
    Image.MAX_IMAGE_PIXELS = None
    images = convert_from_bytes(pdf_bytes, dpi=250)
    page_b64s = []
    for i, img in enumerate(images):
        print(f"Preprocessing page {i+1}/{len(images)}")
        processed = preprocess_image(img)
        b64 = img_to_b64(processed, quality=92)
        size_kb = len(base64.b64decode(b64)) / 1024
        print(f"Page {i+1}: {size_kb:.0f}KB")
        page_b64s.append(b64)
    return page_b64s

def prepare_image(file_bytes):
    img = Image.open(io.BytesIO(file_bytes))
    processed = preprocess_image(img)
    return img_to_b64(processed, quality=92)

# ── Claude calls ─────────────────────────────────────────────────────────────

def claude_call(api_key, images, prompt, max_tokens=2048):
    client = anthropic.Anthropic(api_key=api_key)
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

def claude_text_call(api_key, prompt, max_tokens=4096):
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    )
    return msg.content[0].text.strip()

# ── Validation ────────────────────────────────────────────────────────────────

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

# ── R365 / FTP ────────────────────────────────────────────────────────────────

def get_r365_token():
    resp = httpx.post(
        f"{R365_URL}/APIv1/Authenticate/JWT",
        params={"format": "json"},
        json={"UserName": R365_USER, "Password": R365_PASS},
        headers={"Content-Type": "application/json"},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()["BearerToken"]

def push_to_r365(csv_text, location_code, file_url):
    token = get_r365_token()
    lines = csv_text.strip().split('\n')
    invoice_lines = []
    doc_number = None
    invoice_date = None

    for line in lines:
        if not line.strip() or line.startswith('Vendor,'):
            continue
        cols = line.split(',')
        if len(cols) < 13:
            continue
        if not doc_number:
            doc_number = cols[2].strip()
            invoice_date = cols[3].strip()
        try:
            invoice_lines.append({
                "Product_Number": cols[4].strip(),
                "Quantity": float(cols[7].strip()),
                "Invoice_Line_Item_Cost": float(cols[8].strip()),
                "Extended_Price": float(cols[9].strip()),
                "Product_Description": cols[5].strip(),
                "Unit_Of_Measure": cols[6].strip()
            })
        except (ValueError, IndexError) as e:
            print(f"Skipping line: {e}")
            continue

    invoice_total = sum(l["Extended_Price"] for l in invoice_lines)

    payload = {
        "BatchId": doc_number or "VABC_IMPORT",
        "userId": R365_USER,
        "apInvoices": [{
            "Vendor_Name": "VA ABC",
            "Retailer_Store_Number": str(location_code),
            "Invoice_Date": invoice_date,
            "Invoice_Number": doc_number,
            "Invoice_Amount": invoice_total,
            "Image_URL": file_url,
            "Product_Number": l["Product_Number"],
            "Quantity": l["Quantity"],
            "Invoice_Line_Item_Cost": l["Invoice_Line_Item_Cost"],
            "Extended_Price": l["Extended_Price"],
            "Product_Description": l["Product_Description"],
            "Unit_Of_Measure": l["Unit_Of_Measure"]
        } for l in invoice_lines]
    }

    resp = httpx.post(
        f"{R365_URL}/APIv1/APInvoices",
        json=payload,
        headers={"Authorization": token, "Content-Type": "application/json"},
        timeout=60
    )
    resp.raise_for_status()
    return resp.json()

def upload_to_ftp(filename, content):
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, 21)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    ftp.cwd(FTP_DIR)
    ftp.storbinary(f'STOR {filename}', io.BytesIO(content.encode('utf-8')))
    ftp.quit()

# ── Main route ────────────────────────────────────────────────────────────────

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

    is_pdf = 'pdf' in content_type or file_url.lower().endswith('.pdf')

    if is_pdf:
        try:
            page_b64s = prepare_pages(file_bytes)
        except Exception as e:
            print(f"PDF preprocessing failed: {e}")
            file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            page_b64s = [file_base64]
    else:
        try:
            page_b64s = [prepare_image(file_bytes)]
        except Exception as e:
            print(f"Image preprocessing failed: {e}")
            img = Image.open(io.BytesIO(file_bytes)).convert('RGB')
            img.thumbnail((2000, 2000), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            page_b64s = [base64.standard_b64encode(buf.getvalue()).decode('utf-8')]

    try:
        # 4 parallel Claude calls
        def call_codes():
            return claude_call(api_key, page_b64s,
                DIGIT_AMBIGUITY_GUIDE + """

This is a preprocessed Virginia ABC invoice image.
Look ONLY at the PRODUCT CODE column (leftmost column with 6-digit numbers).

IMPORTANT: Some invoices have faint ghost numbers that are lighter than real codes
and appear on rows with NO corresponding product name. Ignore these ghost codes entirely.
Only include product codes that have a clear, dark product name on the same row.

Read every REAL product code top to bottom across ALL pages.
Return ONLY a numbered list:
1. 011297
2. 015626
No explanation. Numbers only.""")

        def call_names_and_meta():
            return claude_call(api_key, page_b64s,
                """This is a preprocessed Virginia ABC invoice image.
Read every PRODUCT NAME from top to bottom across ALL pages.
Also find the document/order number and pickup date.

IMPORTANT: Only include product names that are clearly printed and dark.
Ignore any faint ghost text. Every name must correspond to a real line item.

Return exactly:
DOCUMENT_NUMBER: [number]
DATE: [date or NONE]
NAMES:
1. crown royal whisky
2. jameson irish whiskey
No explanation. Same count as real product codes.""")

        def call_qty():
            return claude_call(api_key, page_b64s,
                DIGIT_AMBIGUITY_GUIDE + """

This is a preprocessed Virginia ABC invoice image.
Look ONLY at the ORDER QTY column.
Read every quantity top to bottom across ALL pages.
Only include quantities that correspond to real line items with a product name.
Quantities are often multi-digit: 10, 14, 24, 48, 72 are common.
Return ONLY a numbered list:
1. 2
2. 14
No explanation. Numbers only. Same count as product codes.""")

        def call_prices():
            return claude_call(api_key, page_b64s,
                DIGIT_AMBIGUITY_GUIDE + """

This is a preprocessed Virginia ABC invoice image.
Look ONLY at the UNIT PRICE and TOTAL AMOUNT columns.
Read every value top to bottom across ALL pages.
Only include values for real line items with a product name.
Return:
UNIT PRICES:
1. 38.99
2. 31.99
TOTALS:
1. 77.98
2. 447.86
GRAND_TOTAL: 5763.74
No explanation. Numbers only. Same count as product codes.""")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(call_codes): 'codes',
                executor.submit(call_names_and_meta): 'names',
                executor.submit(call_qty): 'qty',
                executor.submit(call_prices): 'prices',
            }
            results = {}
            for future in as_completed(futures):
                key = futures[future]
                results[key] = future.result()

        codes_text = results['codes']
        names_text = results['names']
        qty_text = results['qty']
        prices_text = results['prices']

        code_count = len([l for l in codes_text.strip().split('\n') if l.strip() and l[0].isdigit()])

        merge_prompt = "You are merging data from a Virginia ABC invoice read in separate passes.\n\n"
        merge_prompt += f"PRODUCT CODES ({code_count} real items):\n" + codes_text + "\n\n"
        merge_prompt += "NAMES AND DOCUMENT INFO:\n" + names_text + "\n\n"
        merge_prompt += "QUANTITIES:\n" + qty_text + "\n\n"
        merge_prompt += "UNIT PRICES AND TOTALS:\n" + prices_text + "\n\n"
        merge_prompt += f"CRITICAL: Output must have exactly {code_count} data rows.\n"
        merge_prompt += "Match by position: row 1 code + row 1 name + row 1 qty + row 1 price + row 1 total.\n"
        merge_prompt += "For every row verify: Qty x Unit Price = Total. If not, use Total / Unit Price to correct Qty.\n"
        merge_prompt += "Extract document number and date. If date is NONE use: " + upload_date + "\n\n"
        merge_prompt += "Return as CSV:\n"
        merge_prompt += "Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location\n\n"
        merge_prompt += "Vendor=VA ABC, Location=" + str(location_code) + ", UofM=Bottle for 750ml/Liter for 1L/Each otherwise, "
        merge_prompt += "Qty=X.00, no $ signs, Image URL=" + file_url + ", Break Flag=N, Detail Location=" + str(location_code) + "\n\n"
        merge_prompt += "After last row add: GRAND_TOTAL:[number only]\n"
        merge_prompt += "No header row. No explanation. No markdown."

        final_text = claude_text_call(api_key, merge_prompt)

    except Exception as e:
        print(f"Multi-call failed: {e}, falling back to PDF beta")
        file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.beta.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            betas=["pdfs-2024-09-25"],
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_base64}},
                {"type": "text", "text": "Extract ALL line items as CSV: Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location. Vendor=VA ABC, Location=" + str(location_code) + ", Image URL=" + file_url + ", Break Flag=N, Detail Location=" + str(location_code) + ", Date=" + upload_date + " if not found. Add GRAND_TOTAL at end. No header. No markdown."}
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

    try:
        first_data_line = [l for l in fixed_csv.split('\n') if l and not l.startswith('Vendor,')][0]
        cols = first_data_line.split(',')
        doc_number = cols[2].strip()
        date = cols[3].strip().replace('/', '')
        filename = f"Colin_Export_VABC_{doc_number}_{date}.csv"
    except Exception:
        filename = "Colin_Export_VABC_invoice.csv"

    r365_status = "not attempted"
    ftp_status = "not attempted"

    try:
        push_to_r365(fixed_csv, location_code, file_url)
        r365_status = "success"
        flagged.append("R365 API: Invoice pushed successfully")
    except Exception as e:
        r365_status = f"R365 API error: {str(e)}"
        flagged.append(f"R365 API failed: {str(e)} - falling back to FTP")
        try:
            upload_to_ftp(filename, fixed_csv)
            ftp_status = "success"
            flagged.append("FTP fallback: success")
        except Exception as ftp_e:
            ftp_status = f"FTP error: {str(ftp_e)}"
            flagged.append(f"FTP fallback failed: {str(ftp_e)}")

    return jsonify({
        "result": fixed_csv,
        "filename": filename,
        "r365_status": r365_status,
        "ftp_status": ftp_status,
        "calculated_total": calculated_total,
        "claude_read_total": invoice_total,
        "flagged": flagged
    })


@app.route('/test-odata', methods=['GET'])
def test_odata():
    try:
        resp = httpx.get(
            'https://odata.restaurant365.net/api/v2/views/Item',
            auth=('housepitality\\housepitalityAPI', 'QCE7gdx0wbu_und6kuq'),
            params={'$top': '5', '$select': 'itemId,itemNumber,category1,category2'},
            timeout=15
        )
        return jsonify({"status": resp.status_code, "body": resp.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
