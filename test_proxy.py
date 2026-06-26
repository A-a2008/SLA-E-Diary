import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    base_url="http://localhost:4000/v1",
    api_key="sk-opencode-secret-nothingx16",
)

try:
    response = client.chat.completions.create(
        model="deepseek-ai/deepseek-v4-flash",
        messages=[
            {"role": "user", "content": "Confirm connection. Say 'Connection Successful'"}
        ]
    )
    print(f"Response: {response.choices[0].message.content}")
except Exception as e:
    print(f"Error: {e}")