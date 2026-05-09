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
        max_tokens=2000,
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
                    "text": f"""Extract all line items from this invoice and return them as a CSV with exactly these columns in this order:
 
Vendor,Location,Document Number,Date,Vendor Item Number,Vendor Item Name,UofM,Qty,Unit Price,Total,Image URL,Break Flag,Detail Location
 
Rules:
- Vendor: always "VA ABC"
- Location: always {location_code}
- Document Number: the order number from the invoice
- Date: the pickup date from the invoice in M/D/YYYY format
- Vendor Item Number: the product code
- Vendor Item Name: the product name in lowercase
- UofM: use "Bottle" for 750ml items, "Liter" for 1L items, "Each" for everything else
- Qty: quantity ordered, formatted as X.00
- Unit Price: unit price without $ sign
- Total: total amount without $ sign
- Image URL: leave blank
- Break Flag: always N
- Detail Location: always {location_code}
 
Return only the CSV rows, no header row, no explanation, no markdown."""
                }
            ]
        }]
    )
 
    return jsonify({"result": message.content[0].text})
 
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

