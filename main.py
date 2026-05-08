import anthropic
import base64
import httpx
from flask import Flask, request, jsonify
from pdf2image import convert_from_bytes
import io

app = Flask(__name__)

@app.route('/process', methods=['POST'])
def process_invoice():
    data = request.json
    file_url = data.get('file_url')
    api_key = data.get('api_key')

    # Fetch the file
    file_response = httpx.get(file_url)
    content_type = file_response.headers.get('content-type', '')

    # Convert PDF to image if needed
    if 'pdf' in content_type or file_url.lower().endswith('.pdf'):
        images = convert_from_bytes(file_response.content, dpi=200)
        image_contents = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format='JPEG')
            b64 = base64.standard_b64encode(buf.getvalue()).decode('utf-8')
            image_contents.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
            })
    else:
        b64 = base64.standard_b64encode(file_response.content).decode('utf-8')
        image_contents = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]

    image_contents.append({
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
    })

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": image_contents}]
    )

    return jsonify({"result": message.content[0].text})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
