import anthropic
import base64
import httpx
from flask import Flask, request, jsonify

app = Flask(__name__)

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
                    "text": f"""This document is a photo or scan of a printed invoice. The image quality may vary — it could be perfectly flat, slightly angled, shadowy, or low resolution. Regardless of image quality, extract every line item accurately.

CRITICAL READING INSTRUCTIONS:
- Read each row carefully from left to right, staying on the same horizontal line across the full width of the page even if the image is skewed or distorted.
- Do not let image quality, shadows, or perspective cause you to misread numbers or mix up values from adjacent rows.
- There are likely 40+ line items across multiple pages. Do not stop early. Read every row to the very bottom.
- Quantities are often 2 digits: 10, 14, 24 are common. Never assume a quantity is 1 unless Total equals exactly 1 × Unit Price.
- For EVERY row: multiply Qty × Unit Price. If the result does not match Total, you have misread something. Re-read that row and fix it.
- The Total column is ground truth. If needed, calculate Qty = Total ÷ Unit Price to verify.

Return the data as a CSV with exactly these columns in this order:

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

Return only the CSV rows. No header. No explanation. No markdown."""
                }
            ]
        }]
    )

    return jsonify({"result": message.content[0].text})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
