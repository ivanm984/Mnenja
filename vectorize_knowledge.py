
import os
import json
import psycopg2
import google.generativeai as genai
from dotenv import load_dotenv

# Naložimo okoljske spremenljivke (DATABASE_URL in GEMINI_API_KEY)
load_dotenv()

def connect_db():
    """Vzpostavi povezavo z bazo in vrne povezavo ter kurzor."""
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        return conn, conn.cursor()
    except Exception as e:
        print(f"❌ Napaka pri povezavi z bazo: {e}")
        return None, None

def fetch_knowledge_resources(cursor):
    """Pridobi vire znanja iz tabele knowledge_resources."""
    try:
        cursor.execute("SELECT name, payload FROM knowledge_resources;")
        return cursor.fetchall()
    except Exception as e:
        print(f"❌ Napaka pri pridobivanju virov znanja: {e}")
        return []

def chunk_data(resources):
    """Razdeli JSON podatke v smiselne kose besedila."""
    chunks = []
    print("Prepoznavanje in razdeljevanje virov znanja...")
    for name, payload in resources:
        try:
            # SPREMEMBA: Preverjamo imena brez .json končnice in dodajamo natančno logiko za vsak vir
            if name == 'opn':
                for section, articles in payload.items():
                    if isinstance(articles, dict):
                        for key, content in articles.items():
                            text = f"Vir: {name}, Razdelek: {section}, Ključ: {key}\n\n{json.dumps(content, ensure_ascii=False)}"
                            chunks.append({'vir': name, 'kljuc': f"{section}.{key}", 'vsebina': text})
            elif name == 'priloga1' and 'objects' in payload:
                for item in payload['objects']:
                    text = f"Vir: {name}, Objekt: {item.get('title')}\n\n{json.dumps(item, ensure_ascii=False)}"
                    chunks.append({'vir': name, 'kljuc': f"objekt_{item.get('id')}", 'vsebina': text})
            elif name == 'priloga2' and 'table_entries' in payload:
                for item in payload['table_entries']:
                    text = f"Vir: {name}, Naselje: {item.get('naselje')}, Enota: {item.get('enota_urejanja')}\n\n{json.dumps(item, ensure_ascii=False)}"
                    chunks.append({'vir': name, 'kljuc': item.get('enota_urejanja'), 'vsebina': text})
            elif name == 'priloga3-4':
                if 'priloga3' in payload and 'entries' in payload['priloga3']:
                    for item in payload['priloga3']['entries']:
                        text = f"Vir: {name} (Priloga 3), Naselje: {item.get('ime_naselja')}\n\n{json.dumps(item, ensure_ascii=False)}"
                        chunks.append({'vir': name, 'kljuc': f"p3_{item.get('urejevalna_enota')}", 'vsebina': text})
                if 'priloga4' in payload and isinstance(payload['priloga4'], list):
                    for item in payload['priloga4']:
                        text = f"Vir: {name} (Priloga 4), Naselje: {item.get('ime_naselja')}\n\n{json.dumps(item, ensure_ascii=False)}"
                        chunks.append({'vir': name, 'kljuc': f"p4_{item.get('enote_urejanja_prostora')}", 'vsebina': text})
            elif name == 'izrazi' and 'terms' in payload:
                for item in payload['terms']:
                    text = f"Vir: {name}, Izraz: {item.get('term')}\n\n{json.dumps(item, ensure_ascii=False)}"
                    chunks.append({'vir': name, 'kljuc': item.get('term'), 'vsebina': text})
            elif name == 'uredba':
                # Primer razdeljevanja za Uredbo - lahko se še izboljša
                for key, content in payload.items():
                     text = f"Vir: {name}, Razdelek: {key}\n\n{json.dumps(content, ensure_ascii=False)}"
                     chunks.append({'vir': name, 'kljuc': key, 'vsebina': text})
        except Exception as e:
            print(f"⚠️ Napaka pri obdelavi vira '{name}': {e}")
            
    print(f"\n✅ Pripravljenih {len(chunks)} kosov besedila za vektorizacijo.")
    return chunks

def main():
    """Glavna funkcija za izvedbo vektorizacije."""
    
    conn, cursor = connect_db()
    if not conn:
        return
        
    try:
        cursor.execute("SELECT COUNT(*) FROM vektorizirano_znanje;")
        if cursor.fetchone()[0] > 0:
            if input("Tabela 'vektorizirano_znanje' že vsebuje podatke. Ali želite izbrisati obstoječe in nadaljevati? (da/ne): ").lower() != 'da':
                print("Prekinjeno.")
                return
            print("Brisanje obstoječih podatkov...")
            cursor.execute("TRUNCATE TABLE vektorizirano_znanje RESTART IDENTITY;")
            
        resources = fetch_knowledge_resources(cursor)
        if not resources:
            print("Viri znanja niso bili najdeni v bazi. Najprej zaženite 'migrate_knowledge_base.py'.")
            return
            
        chunks = chunk_data(resources)
        if not chunks:
            print("Ni bilo najdenih kosov za vektorizacijo. Preverite logiko v funkciji chunk_data.")
            return

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("❌ GEMINI_API_KEY ni nastavljen v .env datoteki!")
            return
        genai.configure(api_key=api_key)
        
        print("\nZačenjam z vektorizacijo in shranjevanjem v bazo...")
        model_name = 'models/text-embedding-004'
        
        batch_size = 100 # SPREMEMBA: Vektoriziramo v paketih po 100
        for i in range(0, len(chunks), batch_size):
            batch_chunks = chunks[i:i + batch_size]
            contents_to_embed = [chunk['vsebina'] for chunk in batch_chunks]

            print(f"Vektoriziram paket {i//batch_size + 1}/{(len(chunks) + batch_size - 1)//batch_size} (vnosi {i+1}-{i+len(batch_chunks)})...")
            
            # Klic API-ja za ustvarjanje vdelav za celoten paket
            result = genai.embed_content(model=model_name, content=contents_to_embed, task_type="RETRIEVAL_DOCUMENT")
            embeddings = result['embedding']

            # Shranjevanje v bazo
            for j, chunk in enumerate(batch_chunks):
                cursor.execute(
                    "INSERT INTO vektorizirano_znanje (vir, kljuc, vsebina, vektor) VALUES (%s, %s, %s, %s)",
                    (chunk['vir'], chunk['kljuc'], chunk['vsebina'], embeddings[j])
                )
            
        conn.commit()
        print(f"\n✅ Uspešno vektoriziranih in shranjenih {len(chunks)} virov znanja!")

    except Exception as e:
        print(f"❌ Med procesom je prišlo do napake: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    main()