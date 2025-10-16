# test_app.py
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_homepage():
    response = client.get("/")
    assert response.status_code == 200
    assert "html" in response.text

def test_saved_sessions():
    response = client.get("/saved-sessions")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_extract_data():
    response = client.post("/extract-data", data={"project_text": "Test text"})
    assert response.status_code == 200
    assert "data" in response.json()

def test_ask():
    response = client.post("/ask", json={"question": "Test question", "key_data": {}, "eup": None, "namenska_raba": None})
    assert response.status_code == 200
    assert "answer" in response.json()

# Run with pytest test_app.py