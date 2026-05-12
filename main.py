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

    # Single invoice with all line items grouped together
    payload = {
        "apInvoices": [{
            "Vendor_Name": "VA ABC",
            "Retailer_Store_Number": str(location_code),
            "Invoice_Date": invoice_date,
            "Invoice_Number": doc_number,
            "Invoice_Amount": sum(l["Extended_Price"] for l in invoice_lines),
            "Image_URL": file_url,
            "Invoice_Line_Items": invoice_lines
        }]
    }

    resp = httpx.post(
        f"{R365_URL}/APIv1/APInvoices",
        json=payload,
        headers={
            "Authorization": token,
            "Content-Type": "application/json"
        },
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

    client = anthropic.Anthropic(api_key=api_key)

    is_pdf = 'pdf' in content_type or file_url.lower().endswith('.pdf')

    if is_pdf:
        try:
            raw_images, pages = prepare_pages(file_bytes)
            page_b64s = [p[0] for p in pages]

            # CALL 1: Detect column boundaries
            detect_prompt = "This is a Virginia ABC invoice or receipt image.\n"
            detect_prompt += "Image dimensions: " + str(raw_images[0].size[0]) + "x" + str(raw_images[0].size[1]) + " pixels.\n"
            detect_prompt += """Identify the exact pixel boundaries of these columns on the first page:
- product_code: column with 6-digit item/product codes
- product_name: column with product names
- qty: column with order quantities
- unit_price: column with unit prices
- total: column with total amounts
- data_top: y pixel where data rows start (after header)
- data_bottom: y pixel where data rows end (before footer/totals row)

Return ONLY valid JSON:
{
  "product_code": {"x1": 50, "x2": 150},
  "product_name": {"x1": 155, "x2": 400},
  "qty": {"x1": 405, "x2": 480},
  "unit_price": {"x1": 485, "x2": 580},
  "total": {"x1": 585, "x2": 700},
  "data_top": 120,
  "data_bottom": 900
}
No explanation. JSON only."""

            bounds_text = claude_call(client, [page_b64s[0]], detect_prompt, max_tokens=500)

            try:
                bounds_text = bounds_text.replace('```json', '').replace('```', '').strip()
                bounds = json.loads(bounds_text)
            except Exception as e:
                print(f"Bounds parse error: {e}, using fallback")
                w_img, h_img = raw_images[0].size
                bounds = {
                    "product_code": {"x1": 0, "x2": int(w_img * 0.15)},
                    "product_name": {"x1": int(w_img * 0.15), "x2": int(w_img * 0.55)},
                    "qty": {"x1": int(w_img * 0.55), "x2": int(w_img * 0.65)},
                    "unit_price": {"x1": int(w_img * 0.65), "x2": int(w_img * 0.80)},
                    "total": {"x1": int(w_img * 0.80), "x2": w_img},
                    "data_top": int(h_img * 0.10),
                    "data_bottom": int(h_img * 0.95)
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
            qty_b64 = make_crop('qty', scale=4)
            price_b64 = make_crop('unit_price', scale=4)
            total_b64 = make_crop('total', scale=4)

            # CALL 2: Product codes
            codes_text = claude_call(
                client,
                [codes_b64],
                DIGIT_AMBIGUITY_GUIDE + """

This is a high-resolution crop of ONLY the product code column from a Virginia ABC invoice.
Read every product code from top to bottom. There may be items on multiple pages.
Return only a numbered list:
1. 011297
2. 015626
No explanation. Numbers only."""
            )

            # CALL 3: Names + doc info
            names_text = claude_call(
                client,
                page_b64s,
                """This is a Virginia ABC invoice or receipt.
Read every product name from top to bottom. Include ALL items on ALL pages.
Also find the document/order number and date.
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

This is a high-resolution crop of ONLY the order quantity column.
Read every quantity from top to bottom. Include ALL rows.
Quantities are often multi-digit: 10, 14, 24 are common.
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

These are high-resolution crops of the UNIT PRICE and TOTAL AMOUNT columns.
Read every value from top to bottom. Include ALL rows.
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

            # Merge prompt
            merge_prompt = "You are merging data from a Virginia ABC invoice read in separate passes.\n\n"
            merge_prompt += "PRODUCT CODES:\n" + codes_text + "\n\n"
            merge_prompt += "NAMES AND DOCUMENT INFO:\n" + names_text + "\n\n"
            merge_prompt += "QUANTITIES:\n" + qty_text + "\n\n"
            merge_prompt += "UNIT PRICES AND TOTALS:\n" + prices_text + "\n\n"
            merge_prompt += "CRITICAL: You must include ALL line items. Match row 1 code with row 1 name with row 1 qty etc.\n"
            merge_prompt += "Do not skip any rows. The number of output rows must equal the number of product codes listed above.\n"
            merge_prompt += "For every row verify: Qty x Unit Price = Total. If not, use Total / Unit Price to correct Qty.\n"
            merge_prompt += "Extract document number and date from NAMES AND DOCUMENT INFO. If date is NONE use: " + upload_date + "\n\n"
            merge_prompt += "Return as CSV with these exact columns:\n"
            merge_prompt += "Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location\n\n"
            merge_prompt += "Vendor=VA ABC, Location=" + str(location_code) + ", UofM=Bottle for 750ml/Liter for 1L/Each otherwise, "
            merge_prompt += "Qty=X.00, no $ signs, Image URL=" + file_url + ", Break Flag=N, Detail Location=" + str(location_code) + "\n\n"
            merge_prompt += "After last row add: GRAND_TOTAL:[number only]\n"
            merge_prompt += "No header row. No explanation. No markdown."

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
                    {"type": "text", "text": "Extract ALL line items as CSV: Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location. Vendor=VA ABC, Location=" + str(location_code) + ", Image URL=" + file_url + ", Break Flag=N, Detail Location=" + str(location_code) + ", Date=" + upload_date + " if not found. Add GRAND_TOTAL at end. No header. No markdown."}
                ]}]
            )
            final_text = msg.content[0].text.strip()

    else:
        file_base64 = base64.standard_b64encode(file_bytes).decode('utf-8')
        receipt_prompt = DIGIT_AMBIGUITY_GUIDE + "\n"
        receipt_prompt += "Extract ALL line items from this Virginia ABC receipt as CSV.\n"
        receipt_prompt += "Columns: Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location\n"
        receipt_prompt += "Vendor=VA ABC, Location=" + str(location_code) + ", Date=" + upload_date + " if not found, Image URL=" + file_url + ", Break Flag=N, Detail Location=" + str(location_code) + "\n"
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

    try:
        first_data_line = [l for l in fixed_csv.split('\n') if l and not l.startswith('Vendor,')][0]
        cols = first_data_line.split(',')
        doc_number = cols[2].strip()
        date = cols[3].strip().replace('/', '')
        filename = f"Colin_Export_VABC_{doc_number}_{date}.csv"
    except Exception:
        filename = "Colin_Export_VABC_invoice.csv"

    # Try R365 API first, fall back to FTP
    r365_status = "not attempted"
    ftp_status = "not attempted"

    try:
        r365_response = push_to_r365(fixed_csv, location_code, file_url)
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
 
