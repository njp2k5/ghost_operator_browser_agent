import httpx

r = httpx.post("http://127.0.0.1:8000/generate-link", json={
    "user_id": "test-replan",
    "task": "Sign up for a Shopify account to create an online store",
    "target_url": "https://accounts.shopify.com/signup"
}, timeout=60)
print(r.status_code)
print(r.json())
