import httpx

task = (
    "Search for remote Python developer jobs on Indeed.com. "
    "Fill in 'Python Developer' as the job title and 'Remote' as the location, "
    "then click Find Jobs. After seeing results, use the 'Job Type' filter to select "
    "'Full-time', then use the 'Date posted' filter to show only jobs from the last "
    "7 days. Finally, click the top job listing to read the full job description."
)

r = httpx.post("http://127.0.0.1:8000/generate-link", json={
    "user_id": "demo-user",
    "task": task,
    "target_url": "https://www.indeed.com"
}, timeout=60)
print("Status:", r.status_code)
print("Body:", r.text)
