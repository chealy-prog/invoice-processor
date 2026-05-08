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

    # Fetch the file
    file_response = httpx.get(file_url)
    file_base64 = base64.standard_b64encode(file_response.content).decode('utf-8')
    mime_type = file_response.headers.get('content-type', 'image/jpeg')

    # Send to Claude
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": file_base64
                    }
                },
                {
                    "type": "text",
                    "text": """Extract all data from this invoice. Return two sections:

---META---
Report Date:
Order Number:
Pickup Date:
Licensee Number:
Trade Name:
Address:
Phone:

---CSV---
Product Code,Product Name,Size,Order Qty,Unit Price,Total Amount

End with a totals row. Calculate the grand total by summing all line items. Return nothing else."""
                }
            ]
        }]
    )

    return jsonify({ "result": message.content[0].text })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
