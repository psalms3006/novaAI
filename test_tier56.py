import base64
from groq import Groq

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")

# Using your verified root image
image_base64 = encode_image("2.jpg")  

# Explicitly pass your key right here
client = Groq(api_key="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")

response = client.chat.completions.create(
    model="meta-llama/llama-4-scout-17b-16e-instruct",  # <-- Updated Model ID
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image in detail."},
                {
                    "type": "image_url", 
                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                }
            ]
        }
    ]
)

print(response.choices[0].message.content)