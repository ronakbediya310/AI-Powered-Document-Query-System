import os
import json
import time
import pymysql
import re
from django.db.models import Count, Q
import pickle
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, FileResponse
from django.shortcuts import get_object_or_404
from .models import SourceDocument, QueryHistory
from .models import UnansweredQuestion
from django.core.files import File
from django.core.cache import cache
from docx import Document
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.core.files.storage import default_storage
from langchain_groq import ChatGroq
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import PyPDFLoader, CSVLoader, JSONLoader
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.prompts import ChatPromptTemplate
from langchain.chains import LLMChain
from langchain.chains.combine_documents import create_stuff_documents_chain
from dotenv import load_dotenv
from langchain.memory import ConversationBufferWindowMemory
from langchain.chains import ConversationalRetrievalChain
from django.utils.timezone import now
from datetime import timedelta
from rest_framework.response import Response
from rest_framework.decorators import api_view
import uuid
from django.conf import settings
from .models import APIToken, AdminUser
from django.utils import timezone
from django.utils.timezone import now
from django.db import transaction
from rest_framework import status
from rest_framework import serializers
from langchain.schema import Document
import pandas as pd
from datetime import datetime
from django.db.models import Max
from django.utils.dateparse import parse_datetime
from django.utils.timezone import make_aware
import logging

# Setup logging
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "backend.log"),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

## Initialization
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
llm = ChatGroq(groq_api_key=GROQ_API_KEY, model="llama-3.3-70b-versatile")

VECTOR_STORE = None
FAISS_INDEX_PATH = "faiss_index_sales.pkl"

MYSQL_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("SALES_DB_NAME"),
    "port": int(os.getenv("DB_PORT", 3306)),
}
## Hugging face embedding model to convert text chunks into numerical vectors.
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
standard_response_prompt = ChatPromptTemplate.from_template(
    """
    You are a customer support AI assistant for a web hosting company. You assist customers with queries related to hosting services, pricing, plan upgrades, service cancellations, migrations, and invoice generation.

    **Response Guidelines:**

    1. **Supported Topics:**
       Respond accurately and clearly to customer queries related to:
       - Available hosting services and features
       - Pricing and plan comparisons
       - Plan upgrades or downgrades
       - Service cancellation procedures
       - Website or data migration assistance
       - Invoice generation and billing issues

    2. **Non-Technical or Out-of-Scope Queries:**
       - If a question is unrelated to hosting, billing, or technical support, respond with:
         "Sorry, I am not able to answer this. If you wish, I can connect you with an agent..."
         - If the user agrees, respond with: "Connecting to an agent..."

    3. **Response Style:**
       - Provide **clear, concise, and friendly** responses.
       - Do **not** include phrases like “According to the provided context.”
       - Always maintain a **professional and helpful tone**.

    4. **Security and Privacy:**
       - Do **not** disclose or reference any API keys, passwords, or personal user data.

    5. **Knowledge Use:**
       - If the answer is not directly found in the context, provide a **well-informed** response based on your internal knowledge of hosting services and policies.

    6. **Language Support:**
       - If the user requests a response in another language, reply in that language first.
       - Follow it with an English version starting with: **"In English:"**

    7. **Gratitude Handling:**
       - If the user expresses thanks or appreciation, respond with:
         "You're very welcome! If you ever need further assistance, feel free to reach out."

    **Chat History:**
    {chat_history}

    **<context>**
    {context}
    **</context>**

    **User Question:** {question}
    """
)

regenerate_response_prompt = ChatPromptTemplate.from_template(
    """
    You are an AI assistant specializing in customer support.  
    The previous answer did not meet the user's expectations. Generate a **new, improved, and factually correct** response.

    **Instructions:**  
    - The new answer must **differ significantly** from the previous one, even if the facts remain the same.  
    - Avoid phrases, tone, or structure that match the earlier response.  
    - Keep the answer clear, concise, and easy to understand.  
    - If a non-English language is requested, provide the response in **that language first**, followed by **"In English:"** and the English version.  
    - If the user's language preference is unknown, reply only in English.

    **Chat History (for context):**  
    {chat_history}

    **<context>**
    {context}
    **</context>**

    **User's Question:** {question}
    """
)
support_prompt = ChatPromptTemplate.from_template(
    """
    You are a customer support AI assistant. You assist clients by reviewing the full conversation between the client and staff and provide clear, resolution-based responses.

    **Response Rules:**

    1. **Continuity Check:**
       - Review the full conversation.
       - If the client is following up on a previous issue, respond accordingly.
       - If the client is asking a new question (even if related), focus only on that new query.

    2. **Missing Info:**
       - If the client's message lacks required details, ask a brief and direct follow-up question.

    3. **Resolution-Based Response:**
       - If the issue appears resolved and no further question is asked, reply with:
         **"Thank you."**

    4. **Response Style:**
       - Be **concise and clear** — use the shortest effective response.
       - Maintain a **friendly and professional** tone.
       - Avoid unnecessary explanations unless essential.

    5. **Gratitude Handling:**
       - If the user expresses thanks or appreciation, respond with:
         **"You're very welcome! If you ever need further assistance, feel free to reach out."**

    **<context>**
    {context}
    **</context>**

    **Client Conversation:**
    {question}
    """
)

latest_query = ""
## Memory concept :
user_memory_store = {}
CHAT_HISTORY_LIMIT = 10


def get_user_memory(user_id):
    """Create or retrieve memory for a specific user."""
    if user_id not in user_memory_store:
        user_memory_store[user_id] = ConversationBufferWindowMemory(
            memory_key="chat_history",
            output_key="answer",
            k=CHAT_HISTORY_LIMIT,
            return_messages=True,
        )
    return user_memory_store[user_id]


## Database operations:
def connect_db():
    return pymysql.connect(**MYSQL_CONFIG, cursorclass=pymysql.cursors.DictCursor)


## Storing embedings into DB.
def store_vector_in_db(file_name, chunk_text, embedding):
    logger.info(f"Storing vector for file: {file_name}")
    conn = connect_db()
    try:
        with conn.cursor() as cursor:
            sql = "INSERT INTO vector_store2 (file_name, chunk_text, embedding) VALUES (%s, %s, %s)"
            cursor.execute(sql, (file_name, chunk_text, json.dumps(embedding)))
        conn.commit()
        logger.info(
            f"Vector stored successfully for chunk (length: {len(chunk_text)}) from {file_name}"
        )
    except Exception as e:
        logger.error(f"Error storing vector for {file_name}: {e}", exc_info=True)
        conn.rollback()
    finally:
        conn.close()
        logger.debug("Database connection closed.")


def load_vectors_from_db():
    """Load vectors from MySQL."""
    global VECTOR_STORE
    conn = connect_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT chunk_text, embedding FROM vector_store2 LIMIT 10000"
            )
            data = cursor.fetchall()

        if not data:
            logger.warning("No vector data found in the database.")
            return

        documents = []
        embeddings_list = []

        for d in data:
            try:
                chunk_text = d["chunk_text"]
                embedding = json.loads(d["embedding"])

                if not isinstance(embedding, list):
                    logger.warning(f"Invalid embedding format: {embedding}")
                    continue

                documents.append(chunk_text)
                embeddings_list.append(embedding)
            except Exception as e:
                logger.error(f"Error processing embedding: {e}", exc_info=True)

        logger.info(f"Loaded {len(embeddings_list)} embeddings successfully.")
        if len(embeddings_list) != len(documents):
            logger.warning("Mismatch between documents and embeddings!")

        VECTOR_STORE = FAISS.from_texts(documents, embeddings)
        save_faiss_index()

    except Exception as e:
        logger.error(f"Error loading vectors: {e}", exc_info=True)
    finally:
        conn.close()
        logger.debug("Database connection closed after loading vectors.")


def save_faiss_index():
    """Save the FAISS index to a Pickle file."""
    global VECTOR_STORE
    if VECTOR_STORE is not None:
        with open(FAISS_INDEX_PATH, "wb") as f:
            pickle.dump(VECTOR_STORE, f)
        logger.info("FAISS index saved successfully.")
    else:
        logger.warning("No VECTOR_STORE found to save.")


def load_faiss_index():
    """Load the FAISS index from Pickle if available."""
    global VECTOR_STORE
    if os.path.exists(FAISS_INDEX_PATH):
        with open(FAISS_INDEX_PATH, "rb") as f:
            VECTOR_STORE = pickle.load(f)
        logger.info("FAISS index loaded from Pickle for document QnA data.")
        return True
    logger.info("FAISS index file not found. Will load from DB.")
    return False


def load_index_or_db():
    """Ensures FAISS index is loaded, otherwise loads from DB."""
    global VECTOR_STORE
    if not load_faiss_index():
        load_vectors_from_db()
    success = VECTOR_STORE is not None
    logger.info(f"Index loading {'succeeded' if success else 'failed'}.")
    return success


def extract_json_text_as_chunks(data):
    """Convert each JSON object into a single chunk."""
    try:
        if isinstance(data, list):
            logger.info("Processing JSON data as a list.")
            return [json.dumps(item, ensure_ascii=False) for item in data]
        elif isinstance(data, dict):
            logger.info("Processing JSON data as a dictionary.")
            return [json.dumps(data, ensure_ascii=False)]
        else:
            logger.warning(
                "Unexpected JSON format: Root element is not a dict or list."
            )
            return []
    except Exception as e:
        logger.error(f"Error processing JSON data: {e}")
        return []


def load_csv_text(file_path):
    """Load CSV file and return its content as structured dictionaries."""
    try:
        # Detect delimiter automatically
        logger.info(f"Loading CSV file: {file_path}")
        df = pd.read_csv(file_path, sep=None, engine="python", encoding="utf-8")

        # Convert rows into structured dictionaries
        text_entries = df.to_dict(orient="records")
        logger.info(f"Successfully loaded CSV file with {len(text_entries)} entries.")
        return text_entries
    except Exception as e:
        logger.error(f"Error reading CSV file '{file_path}': {e}")
        return []


def process_new_document(file_path):
    try:
        if not os.path.exists(file_path):
            logger.error(f"File not found: {file_path}")
            return False

        text_entries = []

        if file_path.endswith(".pdf"):
            logger.info(f"Processing PDF file: {file_path}")
            loader = PyPDFLoader(file_path)
            documents = loader.load()  # Load PDF content
            raw_texts = [doc.page_content for doc in documents]

            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000, chunk_overlap=200
            )
            text_entries = text_splitter.split_text("\n".join(raw_texts))
            logger.info(f"Extracted {len(text_entries)} chunks from PDF file.")

        elif file_path.endswith(".csv"):
            logger.info(f"Processing CSV file: {file_path}")
            text_entries = load_csv_text(file_path)
            logger.info(f"Extracted {len(text_entries)} entries from CSV file.")

        elif file_path.endswith(".json"):
            logger.info(f"Processing JSON file: {file_path}")
            with open(file_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
            text_entries = extract_json_text_as_chunks(json_data) if json_data else []
            logger.info(f"Extracted {len(text_entries)} chunks from JSON file.")

        else:
            logger.error(f"Unsupported file format: {file_path}")
            return False

        if not text_entries:
            logger.warning(f"No valid text extracted from: {file_path}")
            return False

        # Store vector embeddings for each text entry
        for text in text_entries:
            text_content = (
                json.dumps(text) if isinstance(text, dict) else text
            )  # Convert dict to string
            embedding = embeddings.embed_query(text_content)
            store_vector_in_db(os.path.basename(file_path), text_content, embedding)

        load_vectors_from_db()

        # Safe file removal after processing
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"File processed and deleted: {file_path}")

        return True
    except Exception as e:
        logger.error(f"Error processing document ({file_path}): {e}")
        return False


def save_unanswered_question(question):
    """Store unanswered questions in the database."""
    if not UnansweredQuestion.objects.filter(question=question).exists():
        UnansweredQuestion.objects.create(question=question)


def get_unanswered_questions():
    return UnansweredQuestion.objects.all()


def get_retrieval_chain(regenerate, user_id, is_whmcs=False):
    """Creates or retrieves a cached retrieval chain with the appropriate prompt for the given user."""
    logger.info(
        f"Getting retrieval chain for user {user_id}, regenerate={regenerate}, is_whmcs={is_whmcs}"
    )

    if not load_index_or_db():
        logger.error("Index or database could not be loaded.")
        return None

    retriever = cache.get("retriever")
    if retriever is None:
        logger.info("Retriever not found in cache, creating a new one.")
        retriever = VECTOR_STORE.as_retriever(search_kwargs={"k": 5})
        cache.set("retriever", retriever, timeout=3600)

    selected_prompt = (
        regenerate_response_prompt if regenerate else standard_response_prompt
    )

    logger.debug(f"Selected prompt: {selected_prompt}")

    if is_whmcs:
        selected_prompt = support_prompt
        logger.info(
            f"WHMCS detected, changing prompt to support prompt: {selected_prompt}"
        )

    memory = get_user_memory(user_id)

    try:
        retrieval_chain_instance = ConversationalRetrievalChain.from_llm(
            llm=llm,
            retriever=retriever,
            combine_docs_chain_kwargs={"prompt": selected_prompt},
            memory=memory,
            return_source_documents=True,
        )
        logger.info(f"Retrieval chain successfully created for user {user_id}")
        return retrieval_chain_instance
    except Exception as e:
        logger.error(f"Error creating retrieval chain for user {user_id}: {e}")
        return None


def store_retrieved_documents(q_id, response, save_results=False):
    """
    Stores retrieved documents in the sales database under a unique query_id.
    Now also stores file_name if available in metadata.
    """
    if not save_results:
        logger.info(
            f"Skipping document storage for query_id {q_id} as save_results is False."
        )
        return None

    folder_name = "media/retrieved_docs"
    os.makedirs(folder_name, exist_ok=True)
    logger.info(f"Created folder {folder_name} if it didn't exist.")

    try:
        with transaction.atomic(using="sales_db"):
            query_id = str(uuid.uuid4())
            logger.info(f"Using query_id {query_id} for storing documents.")

            for i, content in enumerate(response.get("source_documents", []), start=1):
                source_type = "website"
                extracted_title = f"Document_{i}"
                dynamic_url = ""

                # Safely get file_name from metadata, fallback if not present
                file_name = (
                    content.metadata.get("file_name")
                    if hasattr(content, "metadata")
                    else ""
                )
                print(content.metadata)
                file_name = file_name or f"unknown_file_{i}.txt"
                logger.debug(f"File name from metadata: {file_name}")

                try:
                    page_content = content.page_content.strip()

                    if page_content.startswith("{") and page_content.endswith("}"):
                        # Clean potentially malformed JSON
                        cleaned_content = (
                            page_content.replace("\\", "\\\\")
                            .replace("\n", "\\n")
                            .replace("\r", "\\r")
                            .replace("\t", "\\t")
                        )
                        cleaned_content = re.sub(r",(\s*[}\]])", r"\1", cleaned_content)

                        doc_data = json.loads(cleaned_content)

                        extracted_title = doc_data.get("Plan Features", extracted_title)
                        dynamic_url = doc_data.get("Source Link", "")

                        logger.debug(f"Parsed doc_data: {doc_data}")
                        logger.info(
                            f"(Title: {extracted_title}, Source Type: {source_type}, URL: {dynamic_url})"
                        )

                except Exception as e:
                    logger.error(f"Error parsing document content: {e}")
                    logger.debug(f"Raw content: {page_content}")

                # Store in the database
                SourceDocument.objects.using("sales_db").create(
                    query_id=query_id,
                    title=extracted_title,
                    file_path=dynamic_url,
                    content=content.page_content,
                    q_id_id=q_id,
                    source=source_type,
                    file_name=file_name,
                )

            logger.info(f"Documents successfully stored for query_id {query_id}.")
    except Exception as e:
        logger.error(f"Error storing retrieved documents for query_id {q_id}: {e}")

    return query_id


def view_source_document(query_id):
    """
    Fetches all source documents for a given query from the sales database.
    """
    documents = SourceDocument.objects.using("sales_db").filter(query_id=query_id)

    if not documents.exists():
        return JsonResponse({"error": "No documents found for this query"}, status=404)

    # Collect all document contents along with their source types and URLs
    results = [
        {
            "title": doc.title,
            "source": doc.source,
            "file_path": doc.file_path,
        }
        for doc in documents
    ]
    length = len(results)
    return JsonResponse({"total documents": length, "documents": results})


def view_source_document_by_queryId(query_id):
    """
    Fetches all source documents for a given query and returns them as a list.
    """
    documents = SourceDocument.objects.filter(query_id=query_id)

    if not documents.exists():
        return JsonResponse({"error": "No documents found for this query"}, status=404)

    # Collect all document contents along with their source types and URLs
    results = [
        {
            "title": doc.title,
            "source": doc.source,
            "file_path": doc.file_path,
        }
        for doc in documents
    ]
    length = len(results)
    return JsonResponse({"total documents": length, "documents": results})


def download_source_document(query):
    document = get_object_or_404(SourceDocument, query=query)

    if document.file_path:
        return FileResponse(open(document.file_path.path, "rb"), as_attachment=True)
    else:
        return JsonResponse({"error": "No file available"}, status=404)


def get_response(
    user_query,
    TMS_agent="Ronak",
    chat_id="67ecd2dd-c36c-800f-89fc-45b88a4a032e",
    regenerate_flag_short=False,
    is_whmcs=False,
):
    logger.info(
        f"Handling query from TMS_agent={TMS_agent}, chat_id={chat_id}, is_whmcs={is_whmcs}"
    )

    retrieval_chain = get_retrieval_chain(
        regenerate=False, user_id=TMS_agent, is_whmcs=is_whmcs
    )
    if retrieval_chain is None:
        logger.error("No retrieval chain found. No indexed documents available.")
        return JsonResponse(
            {"error": "No indexed documents found. Please upload some files first."},
            status=400,
        )

    if regenerate_flag_short:
        user_query = (
            "can you please provide a short response to my previous question? "
            "You can refer to chat history..."
        )

    start_time = time.process_time()
    try:
        response = retrieval_chain.invoke({"question": user_query})
    except Exception as e:
        logger.error("Error invoking retrieval chain", exc_info=True)
        return JsonResponse(
            {"error": "Failed to process the query. Please try again later."},
            status=500,
        )

    response_time = time.process_time() - start_time
    answer = response.get("answer", "").strip()

    if not answer or any(
        phrase in answer.lower()
        for phrase in [
            "i don't know",
            "i'm not sure",
            "i don't have enough information",
            "no relevant information",
            "not enough info to answer.",
        ]
    ):
        save_unanswered_question(user_query)
        logger.info(f"Unanswered query saved: {user_query}")

    formatted_answer = format_response(answer)

    answer_in_english = formatted_answer
    answer_in_requested_language = ""

    if "In English:" in formatted_answer:
        parts = formatted_answer.split("In English:")
        answer_in_requested_language = parts[1].strip()
        answer_in_english = parts[0].strip() if len(parts) > 1 else ""

    is_answer_starts_with_sorry = answer.startswith(
        "Sorry, I am not able to answer this. If you wish, I can connect you with an agent..."
    )

    try:
        exists = (
            QueryHistory.objects.using("sales_db")
            .filter(query=user_query, TMS_agent=TMS_agent, chat_id=chat_id)
            .exists()
        )
        if exists:
            logger.info("Duplicate query detected, skipping insertion.")
            q_id = (
                QueryHistory.objects.using("sales_db")
                .filter(query=user_query, TMS_agent=TMS_agent, chat_id=chat_id)
                .latest("id")
                .id
            )
        else:
            query_history = QueryHistory.objects.using("sales_db").create(
                query=user_query,
                TMS_agent=TMS_agent,
                chat_id=chat_id,
                llm_answer=answer,
                chat_link=f"https://app.chatra.io/conversations/{chat_id}",
                is_answer_starts_with_sorry=is_answer_starts_with_sorry,
                answer_proximity =0.00
            )
            q_id = query_history.id
            logger.info(
                f"Query stored successfully with id {q_id}, "
                f"is_answer_starts_with_sorry={is_answer_starts_with_sorry}"
            )
    except Exception as e:
        logger.error("Error storing query history", exc_info=True)
        return JsonResponse(
            {"error": f"Failed to store query history: {e}"}, status=500
        )

    try:
        query_id = store_retrieved_documents(q_id, response, True)
        logger.info(f"Documents stored successfully for query_id {query_id}")
    except Exception as e:
        logger.error("Error storing retrieved documents", exc_info=True)
        query_id = None

    return JsonResponse(
        {
            "answer_in_english": answer_in_english,
            "answer_in_requested_language": answer_in_requested_language,
            "response_time": response_time,
            "query_id": query_id,
        },
        safe=False,
    )

def regenerate_response(
    user_query, TMS_agent="Ronak", chat_id="67ecd2dd-c36c-800f-89fc-45b88a4a032e"
):
    """Handles regenerating the response based on the previous answer."""

    logger.info(
        f"Regenerating response for user_query '{user_query}', TMS_agent {TMS_agent}, chat_id {chat_id}"
    )

    retrieval_chain = get_retrieval_chain(regenerate=True, user_id=TMS_agent)
    if retrieval_chain is None:
        logger.error("No retrieval chain found for regeneration.")
        return JsonResponse(
            {
                "success": False,
                "message": "No indexed documents found. Please upload some files first.",
            },
            status=400,
        )

    try:
        start_time = time.process_time()
        response = retrieval_chain.invoke({"question": user_query})
        response_time = time.process_time() - start_time
    except Exception as e:
        logger.error("LLM Regeneration Error", exc_info=True)
        return JsonResponse(
            {
                "success": False,
                "message": "Failed to regenerate response. Please try again later.",
            },
            status=500,
        )

    answer = response.get("answer", "").strip()
    if not answer or any(
        phrase in answer.lower()
        for phrase in [
            "i don't know",
            "i'm not sure",
            "i don't have enough information",
            "no relevant information",
            "not enough info to answer.",
        ]
    ):
        save_unanswered_question(user_query)
        logger.info(f"Unanswered query saved for regeneration: {user_query}")

    formatted_answer = format_response(answer)

    try:
        # Update if exists, else create
        query_history, created = QueryHistory.objects.using(
            "sales_db"
        ).update_or_create(
            query=user_query,
            TMS_agent=TMS_agent,
            chat_id=chat_id,
            defaults={
                "llm_answer": formatted_answer,
                "chat_link": f"https://app.chatra.io/conversations/{chat_id}",
            },
        )
        q_id = query_history.id
        if created:
            logger.info(f"New query history created with id {q_id}")
        else:
            logger.info(f"Existing query history updated with id {q_id}")
    except Exception as e:
        logger.error("Error saving or updating query history", exc_info=True)
        return JsonResponse({"success": False, "message": str(e)}, status=500)

    try:
        query_id = store_retrieved_documents(q_id, response, True)
        logger.info(
            f"Documents stored successfully for regeneration with query_id {query_id}"
        )
    except Exception as e:
        logger.error("Error storing retrieved documents", exc_info=True)
        query_id = None

    answer_in_requested_language = ""
    answer_in_english = formatted_answer

    if "In English:" in formatted_answer:
        parts = formatted_answer.split("In English:")
        answer_in_requested_language = parts[0].strip()
        answer_in_english = parts[1].strip() if len(parts) > 1 else ""

    return JsonResponse(
        {
            "success": True,
            "answer_in_english": answer_in_english,
            "answer_in_requested_language": answer_in_requested_language,
            "response_time": response_time,
            "query_id": query_id,
        },
        safe=False,
    )


def format_response(response):
    """Formats the bot's response beautifully with better readability, links, and table rendering."""

    # Bold and italics
    response = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", response)
    response = re.sub(r"\*(.*?)\*", r"<i>\1</i>", response)

    # Lists
    response = re.sub(r"(\n?[-•*]\s+)", r"<br>\1", response)
    response = re.sub(r"(\n?\d+\.\s+)", r"<br>\1", response)

    # Replace multiple line breaks
    response = re.sub(r"\n{2,}", r"<br><br>", response)

    # Convert URLs to clickable links
    url_pattern = r"(https?://[^\s<>]+)"
    response = re.sub(url_pattern, r'<a href="\1" target="_blank">\1</a>', response)

    # Convert markdown-style tables to HTML
    def convert_table(md_table):
        lines = md_table.strip().split("\n")
        headers = [
            f"<th>{col.strip()}</th>" for col in lines[0].split("|") if col.strip()
        ]
        rows = []
        for row in lines[2:]:  # skip separator line
            cells = [
                f"<td>{cell.strip()}</td>" for cell in row.split("|") if cell.strip()
            ]
            rows.append(f"<tr>{''.join(cells)}</tr>")
        header_html = f"<tr>{''.join(headers)}</tr>"
        table_html = (
            f"<table class='response-table'>{header_html}{''.join(rows)}</table>"
        )
        return table_html

    # Regex for Markdown tables
    response = re.sub(
        r"((?:\|.*\|\n)+)",
        lambda m: convert_table(m.group(1)),
        response,
    )

    return response.strip()


def create_dummy_api_token():
    """Function to create a dummy user and API token."""
    user, created = AdminUser.objects.get_or_create(
        username="Ronak",
    )

    api_token = APIToken.objects.create(
        user=user,
        token="a7222a92-8b10-4050-8b26-894dc6f45918",
        expires_at=now() + timedelta(days=7),
    )

    print(f"Dummy token created: {api_token.token}")
    return api_token.token


def get_user_from_token(token):
    """Validate user and token by comparing request token and DB token."""
    try:
        user = AdminUser.objects.get(username="Ronak")
        api_token = APIToken.objects.select_related("user").get(user=user, token=token)
        if api_token.is_valid():
            return api_token.user
    except APIToken.DoesNotExist:
        return None
    return None


# ## This is the Index view of Django.


def index(request):
    # create_dummy_api_token()
    if not request.session.get("authenticated"):
        return redirect("login")
    if request.method == "POST":
        ## Document Upload section.
        if "document" in request.FILES:
            file = request.FILES["document"]
            file_path = default_storage.save(f"media/{file.name}", file)
            if process_new_document(file_path):
                return render(request, "index2.html", {"message": "Upload successful!"})
            return render(request, "index2.html", {"error": "Upload failed!"})

        elif "fetch" in request.POST and "query" in request.POST:
            return view_source_document_by_queryId(query_id=request.POST["query"])

        # elif "query" in request.POST:
        #     return get_response(request.POST["query"])

    return render(request, "index2.html")


IGNORED_QUERIES = {
    "hello",
    "hi",
    "hey",
    "hola",
    "howdy",
    "hiya",
    "greetings",
    "good morning",
    "good afternoon",
    "good evening",
    "good day",
    "what's up",
    "wassup",
    "sup",
    "yo",
    "how's it going",
    "how are you",
    "how have you been",
    "how are things",
    "i need your help",
    "can you help me",
    "need assistance",
    "help me",
    "i need assistance",
    "assist me",
    "help required",
    "hey there",
    "hi there",
    "hello there",
}
def is_ignored_query(query: str) -> bool:
    query = query.strip().lower()

    if query in IGNORED_QUERIES:
        logger.info(f"Ignored query matched predefined set: {query}")
        return True

    if re.fullmatch(r"\d{5,}", query):
        logger.info(f"Ignored query detected as numeric-only (PIN or ticket): {query}")
        return True

    if re.search(r"(pin|ticket)[^\d]*\d{5,}", query):
        logger.info(f"Ignored query detected as pin/ticket pattern: {query}")
        return True

    if len(query.split()) <= 4 and re.search(r"\d{5,}", query):
        logger.info(f"Ignored query due to short length and numeric pattern: {query}")
        return True

    return False

@csrf_exempt
@api_view(["POST"])
def query_api_sales(request):
    """REST API to validate token and process queries."""

    logger.info("Received request for query API sales.")

    try:
        # Parse the incoming JSON data
        data = json.loads(request.body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON format received.")
        return Response({"error": "Invalid JSON format"}, status=400)

    # Extracting data from the parsed JSON
    token = data.get("token") or request.headers.get("Authorization")
    user_query = data.get("query")
    agent_name = data.get("agent_name")
    chat_id = data.get("chat_id")
    regenerate_flag = str(data.get("regenerate", "false")).lower() == "true"
    regenerate_flag_short = str(data.get("regenerate_short", "false")).lower() == "true"

    # Logging the extracted data
    logger.info(
        f"Extracted parameters: token={token}, user_query={user_query}, agent_name={agent_name}, chat_id={chat_id}"
    )

    # Validate required fields
    if not token or not user_query:
        logger.warning("Missing token or user query.")
        return Response({"error": "Token and query are required."}, status=400)

    # Validate user from token
    user = get_user_from_token(token)
    if not user:
        logger.warning(f"Invalid or expired token: {token}")
        return Response({"error": "Invalid or expired token."}, status=403)

    # Check for ignored queries
    if is_ignored_query(user_query):
        logger.info(f"Ignored or uninformative query received: {user_query}")
        return Response(
            {"answer_in_english": "Could you please provide more context so I can assist better?"}, 
            status=200
        )

    # Handle regeneration of response or normal response generation
    if regenerate_flag:
        logger.info("Regenerating response.")
        return regenerate_response(user_query, agent_name, chat_id)
    else:
        logger.info("Generating response.")
        return get_response(
            user_query, agent_name, chat_id, regenerate_flag_short, is_whmcs=False
        )


@csrf_exempt
@api_view(["POST"])
def query_api_whmcs(request):
    """REST API to validate token and process queries."""
    print("whmcs")
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return Response({"error": "Invalid JSON format"}, status=400)

    token = data.get("token") or request.headers.get("Authorization")
    user_query = data.get("query")
    agent_name = data.get("agent_name")
    chat_id = data.get("chat_id")
    regenerate_flag = str(data.get("regenerate", "false")).lower() == "true"
    regenerate_flag_short = str(data.get("regenerate_short", "false")).lower() == "true"

    if not token or not user_query:
        return Response({"error": "Token and query are required."}, status=400)

    user = get_user_from_token(token)

    if not user:
        return Response({"error": "Invalid or expired token."}, status=403)

    if user_query.strip().lower() in IGNORED_QUERIES:
        return Response(
            {"answer_in_english": f"Hello, how can I assist you today?"},
            status=200,
        )

    if regenerate_flag:
        return regenerate_response(user_query, agent_name, chat_id)
    else:
        return get_response(
            user_query, agent_name, chat_id, regenerate_flag_short, is_whmcs=True
        )


@csrf_exempt
@api_view(["POST"])
def fetch_documents_by_query_id(request):
    """
    API to fetch documents using query_id.
    """
    data = json.loads(request.body)
    query_id = data.get("query_id")
    token = data.get("token") or request.headers.get("Authorization")

    user = get_user_from_token(token)
    if not token or not query_id:
        return Response({"error": "Token and query_id are required."}, status=400)

    if not user:
        return Response({"error": "Invalid or expired token."}, status=403)

    try:
        return view_source_document(query_id)
    except SourceDocument.DoesNotExist:
        return Response({"error": "No documents found for this query_id"}, status=404)

@api_view(["POST"])
def flag_relevance(request):
    try:
        logger.info("[API CALL] /flag-relevance invoked")

        data = json.loads(request.body)
        logger.debug(f"[DATA RECEIVED] {json.dumps(data)}")

        flag = data.get("flag")
        query_id = data.get("query_id")
        answer_by_team = data.get("answer_by_team", "").strip()
        token = data.get("token") or request.headers.get("Authorization")

        if flag is not True:
            logger.warning("[VALIDATION] 'flag' must be True")
            return Response({"error": "Flag must be True"}, status=status.HTTP_400_BAD_REQUEST)

        if not token or not query_id:
            logger.warning("[VALIDATION] Missing 'token' or 'query_id'")
            return Response({"error": "Token and query_id are required."}, status=400)

        user = get_user_from_token(token)
        if not user:
            logger.warning(f"[AUTH] Invalid or expired token: {token}")
            return Response({"error": "Invalid or expired token."}, status=403)

        if not answer_by_team:
            logger.warning("[VALIDATION] 'answer_by_team' is empty")
            return Response(
                {"error": "answer_by_team cannot be empty"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(f"[PROCESSING] Starting DB update for query_id={query_id}")

        with transaction.atomic(using="sales_db"):
            # Step 1: Update relevant=False for all matching docs
            updated_count = SourceDocument.objects.filter(query_id=query_id).update(relevant=False)
            logger.info(f"[UPDATE] SourceDocument marked irrelevant for query_id={query_id} (rows affected: {updated_count})")

            # Step 2: Fetch latest locked SourceDocument
            existing_doc = (
                SourceDocument.objects.using("sales_db")
                .select_for_update()
                .filter(query_id=query_id)
                .last()
            )

            if not existing_doc:
                logger.error(f"[NOT FOUND] SourceDocument not found for query_id={query_id}")
                return Response(
                    {"error": "Query ID not found in SourceDocument."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            logger.info(f"[FOUND] Locked SourceDocument: id={existing_doc.id}, q_id_id={existing_doc.q_id_id}")


            qh_updated = QueryHistory.objects.filter(
                id=existing_doc.q_id_id,
                
            ).update(
                status="Unresolved",
                answer_by_team=answer_by_team,
                is_flagged=True
            )
            logger.info(f"[UPDATE] QueryHistory updated for q_id_id={existing_doc.q_id_id} (rows affected: {qh_updated})")

        return Response(
            {"message": "Successfully updated the document"},
            status=status.HTTP_200_OK,
        )

    except Exception as e:
        logger.exception("[EXCEPTION] Unexpected error in /flag-relevance")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(["POST"])
def store_team_answer(request):
    try:
        logger.info("[API CALL] /store-team-answer invoked")

        data = json.loads(request.body)
        logger.debug(f"[DATA RECEIVED] {json.dumps(data)}")

        query = data.get("query")
        answer_by_team = (data.get("answer_by_team") or "").strip()
        agent_name = (data.get("agent_name") or "").strip()
        chat_id = (data.get("chat_id") or "").strip()
        token = data.get("token") or request.headers.get("Authorization")

        if not token or not query:
            logger.warning("[VALIDATION] Missing 'token' or 'query'")
            return Response({"error": "Token and query are required."}, status=400)

        user = get_user_from_token(token)
        if not user:
            logger.warning(f"[AUTH] Invalid or expired token: {token}")
            return Response({"error": "Invalid or expired token."}, status=403)

        if not answer_by_team:
            logger.warning("[VALIDATION] 'answer_by_team' is empty")
            return Response({"error": "answer_by_team cannot be empty"}, status=400)

        if not chat_id or not agent_name:
            logger.warning("[VALIDATION] Missing 'chat_id' or 'agent_name'")
            return Response({"error": "chat_id and agent_name are required."}, status=400)

        logger.info(f"[PROCESSING] Starting DB update for query={query}")
        qh_updated = QueryHistory.objects.filter(
            query=query,
            chat_id=chat_id,
            TMS_agent=agent_name
        ).update(
            answer_by_team=answer_by_team,
        )

        if qh_updated == 0:
            logger.warning(f"[DB] No matching record found for query={query}, chat_id={chat_id}, agent_name={agent_name}")
            return Response({"error": "No matching record found to update."}, status=404)

        return Response({"message": "Successfully updated the Team Answer"}, status=200)

    except Exception as e:
        logger.exception("[EXCEPTION] Unexpected error")
        return Response({"error": str(e)}, status=500)
    
    
@csrf_exempt
@api_view(["POST"])
def update_query_history_flags(request):
    data = request.data

    logger.info(f"[API CALL] Incoming request data: {json.dumps(data)}")

    # Extract fields
    query_text = data.get("query")
    is_used = data.get("is_used", False)
    is_edited = data.get("is_edited", False)
    answer_proximity = data.get("answer_proximity")
    chat_id = data.get("chat_id")

    # Validation
    if not query_text:
        logger.warning("[VALIDATION] Missing 'query' field.")
        return Response({"error": "Query is required."}, status=status.HTTP_400_BAD_REQUEST)

    if not isinstance(is_used, bool):
        logger.warning(f"[VALIDATION] 'is_used' is not a boolean: {is_used}")
        return Response({"error": "is_used must be a boolean."}, status=status.HTTP_400_BAD_REQUEST)

    if not isinstance(is_edited, bool):
        logger.warning(f"[VALIDATION] 'is_edited' is not a boolean: {is_edited}")
        return Response({"error": "is_edited must be a boolean."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate proximity only if edited
    
    try:
        answer_proximity = float(answer_proximity)
        if not (0 <= answer_proximity <= 100):
            raise ValueError
    except (TypeError, ValueError):
            logger.warning(f"[VALIDATION] Invalid 'answer_proximity': {answer_proximity}")
            return Response({
                "error": "answer_proximity must be a number between 0 and 100 when is_edited is True."
            }, status=status.HTTP_400_BAD_REQUEST)

    try:
        query_obj = (
            QueryHistory.objects.filter(query=query_text, chat_id=chat_id)
            .order_by("-timestamp")
            .first()
        )

        if not query_obj:
            logger.error(f"[QUERY] QueryHistory not found for query='{query_text}' and chat_id='{chat_id}'")
            return Response({"error": "Query not found."}, status=status.HTTP_404_NOT_FOUND)

        # Update DB if answer_proximity is None
        if query_obj.answer_proximity is None:
            logger.info(f"[FIX] 'answer_proximity' was None, setting to 0.00 in DB for query='{query_text}'")
            query_obj.answer_proximity = 0.00
            query_obj.save(update_fields=["answer_proximity"])
            
        changes_made = False

        if is_used and not query_obj.is_used:
            logger.info(f"[UPDATE] Setting 'is_used' to True for query='{query_text}'")
            query_obj.is_used = True
            changes_made = True

        if is_edited and not query_obj.is_edited:
            logger.info(f"[UPDATE] Setting 'is_edited' to True for query='{query_text}'")
            query_obj.is_edited = True
            changes_made = True

        existing_proximity = float(query_obj.answer_proximity)
        if existing_proximity != float(answer_proximity):
            logger.info(f"[UPDATE] Updating 'answer_proximity' from {existing_proximity} to {answer_proximity}")
            query_obj.answer_proximity = answer_proximity
            changes_made = True

        if changes_made:
            query_obj.timestamp = timezone.now()
            query_obj.save()
            logger.info(f"[SUCCESS] Query history updated for: '{query_text}'")
        else:
            logger.info(f"[NO CHANGE] No fields changed for query='{query_text}'")

        return Response({"success": True, "message": "Query history updated successfully."})

    except Exception as e:
        logger.exception("[EXCEPTION] Unexpected error while updating query history.")
        return Response(
            {"error": f"Unexpected error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


## handeling irrelavant docs:


class QueryHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = QueryHistory
        fields = "__all__"


class SourceDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SourceDocument
        fields = "__all__"


def chat_details_view(request, chat_id):
    # Fetch all queries for statistics
    all_queries_for_chat = QueryHistory.objects.using("sales_db").filter(
        chat_id=chat_id
    )

    if not all_queries_for_chat.exists():
        # No queries at all for this chat_id
        stats = {
            "total_queries": 0,
            "used_queries": 0,
            "edited_queries": 0,
            "flagged_queries": 0,
            "resolved_queries": 0,
            "unresolved_queries": 0,
        }

        context = {
            "success": True,
            "chat_id": str(chat_id),
            "queries": [],  # empty since no queries at all
            "irrelevant_documents": [],
            "statistics": stats,
        }

        return JsonResponse(context)

    # Filter only flagged queries for display
    flagged_queries = all_queries_for_chat.filter(is_flagged = True)

    docs = SourceDocument.objects.filter(
        q_id__chat_id=chat_id
    )

    # Serialize data
    serialized_queries = QueryHistorySerializer(all_queries_for_chat, many=True).data
    serialized_docs = SourceDocumentSerializer(docs, many=True).data

    # Statistics calculated from ALL queries
    stats = {
        "total_queries": all_queries_for_chat.count(),
        "used_queries": all_queries_for_chat.filter(is_used=True).count(),
        "edited_queries": all_queries_for_chat.filter(is_edited=True).count(),
        "flagged_queries": flagged_queries.count(),
        "resolved_queries": all_queries_for_chat.filter(status="Resolved").count(),
        "unresolved_queries": all_queries_for_chat.filter(status="Unresolved").count(),
    }

    context = {
        "success": True,
        "chat_id": str(chat_id),
        "chat_link": f"https://app.chatra.io/conversations/{chat_id}",
        "queries": serialized_queries,  # could be empty if no flagged queries
        "documents": serialized_docs,
        "statistics": stats,
    }


    return JsonResponse(context)


@api_view(["GET"])
def list_chat_ids(request):
    from_date_str = request.GET.get("from_date")
    to_date_str = request.GET.get("to_date")

    try:
        # Fetch min and max timestamp from DB
        qs_all = QueryHistory.objects.using("sales_db").all()
        min_timestamp = (
            qs_all.order_by("timestamp").first().timestamp
            if qs_all.exists()
            else datetime.today()
        )
        max_timestamp = (
            qs_all.order_by("-timestamp").first().timestamp
            if qs_all.exists()
            else datetime.today()
        )

        if from_date_str:
            from_date = make_aware(datetime.strptime(from_date_str, "%Y-%m-%d"))
        else:
            from_date = min_timestamp

        if to_date_str:
            to_date = make_aware(
                datetime.strptime(to_date_str, "%Y-%m-%d")
                + timedelta(days=1)
                - timedelta(seconds=1)
            )
        else:
            to_date = max_timestamp
    except ValueError:
        return Response(
            {"error": "Invalid date format. Use YYYY-MM-DDTHH:MM"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Filtered QuerySet based on the date range
    qs = QueryHistory.objects.using("sales_db").filter(
        timestamp__range=(from_date, to_date)
    )

    # Distinct chat_ids
    chat_ids = qs.values("chat_id").distinct()

    # Query counts based on status
    chat_ids_with_unresolved = (
        qs.filter(status="Unresolved").values_list("chat_id", flat=True).distinct()
    )
    resolved_chat_ids = chat_ids.exclude(chat_id__in=chat_ids_with_unresolved)
    chat_ids_with_unresolved = (
        qs.filter(status="Unresolved")
        .values("chat_id")
        .annotate(latest_timestamp=Max("timestamp"))
        .order_by("-latest_timestamp")
    )

    # Get resolved chat_ids (i.e., not in unresolved list)
    unresolved_ids_set = set(chat["chat_id"] for chat in chat_ids_with_unresolved)

    chat_ids_resolved = (
        qs.exclude(chat_id__in=unresolved_ids_set)
        .values("chat_id")
        .annotate(latest_timestamp=Max("timestamp"))
        .order_by("-latest_timestamp")
    )

    # Add missing statistics for all queries
    total_queries = qs.count()
    used_queries = qs.filter(is_used=True).count()
    edited_queries = qs.filter(is_edited=True).count()
    flagged_queries = qs.filter(is_flagged=True).count()
    unresolved_queries = qs.filter(status="Unresolved").count()
    resolved_queries = qs.filter(status="Resolved").count()

    # Prepare statistics
    stats = {
        "from_date": from_date,
        "to_date": to_date,
        "total_chat_id": chat_ids.count(),
        "resolved_chat_id": resolved_chat_ids.count(),
        "total_queries": total_queries,
        "used_queries": used_queries,
        "edited_queries": edited_queries,
        "flagged_queries": flagged_queries,
        "resolved_queries": resolved_queries,
        "unresolved_queries": unresolved_queries,
    }

    # Combine: unresolved first, then resolved
    chat_ids = list(chat_ids_with_unresolved) + list(chat_ids_resolved)
    return Response(
        {
            "statistics": stats,
            "chat_ids": [{"chat_id": chat["chat_id"]} for chat in chat_ids],
        },
        status=status.HTTP_200_OK,
    )


def chat_ids(request):
    if not request.session.get("authenticated"):
        return redirect("login")
    return render(request, "chat_id_list_sales.html")


def search_chat_by_id(request):
    chat_id = request.GET.get("chat_id")
    if not chat_id:
        return JsonResponse({"success": False, "message": "Chat ID is required"})

    # Get the queryset
    chat_queryset = QueryHistory.objects.filter(chat_id=chat_id, is_flagged=True)

    if not chat_queryset.exists():
        return JsonResponse({"success": False, "message": "Chat ID not found"})

    total_queries = chat_queryset.count()
    resolved_queries = chat_queryset.filter(~Q(status="Resolved")).count()
    unresolved_queries = total_queries - resolved_queries

    response_data = {
        "chat_id": chat_id,
        "total_queries": total_queries,
        "resolved_queries": resolved_queries,
        "unresolved_queries": unresolved_queries,
    }

    return JsonResponse({"success": True, "data": response_data})


@api_view(["GET"])
def high_proxy_report(request):
    chats = QueryHistory.objects.using("sales_db").filter(
        answer_proximity__gte=85,
        answer_proximity__lte=100,
    ).values("TMS_agent", "chat_id").distinct()

    return JsonResponse(list(chats), safe=False)
@csrf_exempt
@api_view(["POST"])
def update_query_status(request):
    logger.info("Received request to update query status")

    query_id = request.data.get("query_id")
    new_status = request.data.get("status")
    remarks = request.data.get("remarks", "")

    logger.info(f"Parsed data - Query ID: {query_id}, New Status: {new_status}, Remarks: {remarks}")

    if not query_id or not new_status:
        logger.error("Missing query_id or new_status in the request")
        return Response(
            {"error": "Query ID and Status are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        query = get_object_or_404(QueryHistory, id=query_id)
        logger.info(f"Query found: {query}")

        # Updating fields
        query.status = new_status.capitalize()
        query.remarks = remarks
        query.save()
        logger.info(f"Query updated successfully with status '{query.status}' and remarks.")

        # Update source documents if resolved
        if query.status == "Resolved":
            updated_docs = SourceDocument.objects.filter(q_id_id=query_id).update(relevant=1)
            logger.info(f"Updated {updated_docs} source document(s) as relevant.")

        return Response(
            {"success": True, "message": "Query updated successfully"},
            status=status.HTTP_200_OK,
        )

    except Exception as e:
        logger.exception(f"Error occurred while updating query status: {str(e)}")
        return Response(
            {"error": "Failed to update query status", "details": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

def show_unresolved_queries(request):
    # Fetch all unresolved queries
    unresolved_queries = QueryHistory.objects.using("sales_db").filter(status="Unresolved")

    if not unresolved_queries.exists():
        stats = {
            "total_queries": 0,
            "used_queries": 0,
            "edited_queries": 0,
            "flagged_queries": 0,
            "resolved_queries": 0,
            "unresolved_queries": 0,
        }

        context = {
            "success": True,
            "queries": [],
            "documents": [],
            "statistics": stats,
        }
        return JsonResponse(context)

    # Fetch all documents (or filter if needed)
    docs = SourceDocument.objects.using("sales_db").all()

    # Serialize data
    serialized_queries = QueryHistorySerializer(unresolved_queries, many=True).data
    serialized_docs = SourceDocumentSerializer(docs, many=True).data

    # Compute stats across all queries
    stats = {
        "total_queries": unresolved_queries.count(),
        "used_queries": unresolved_queries.filter(is_used=True).count(),
        "edited_queries": unresolved_queries.filter(is_edited=True).count(),
        "flagged_queries": unresolved_queries.filter(is_flagged=True).count(),
        "resolved_queries": unresolved_queries.filter(status="Resolved").count(),
        "unresolved_queries": unresolved_queries.count(),
    }

    context = {
        "success": True,
        "queries": serialized_queries,
        "documents": serialized_docs,
        "statistics": stats,
    }

    return JsonResponse(context)

def preprocess_text(text):
    """
    Preprocess the text by:
    - Stripping leading/trailing spaces.
    - Normalizing spaces (replace multiple spaces with a single space).
    - Removing special characters.
    - Converting the text to lowercase for case-insensitive comparison.
    """

    text = text.strip()
    text = re.sub(r"\s+", " ", text)  # Normalize spaces
    text = re.sub(r"[^\w\s]", "", text)  # Remove special characters
    text = text.lower()  # Convert to lowercase
    return text


@csrf_exempt
def regenerate_embedding(request):
    if request.method == "POST":
        try:
            # Extract data from the request body
            data = json.loads(request.body)
            query_id = data.get("query_id")
            doc_id = data.get("doc_id")
            new_content = data.get("new_content")

            # Log the received data
            logger.info(
                f"Received request to regenerate embedding for doc_id: {doc_id}, query_id: {query_id}"
            )

            # Fetch the source document from the database
            try:
                document = SourceDocument.objects.get(id=doc_id, q_id=query_id)
                logger.info(
                    f"Document with id {doc_id} and query_id {query_id} fetched successfully."
                )
            except SourceDocument.DoesNotExist:
                logger.error(
                    f"Document with id {doc_id} and query_id {query_id} not found."
                )
                return JsonResponse({"success": False, "error": "Document not found"})

            # Preprocess the new content
            preprocessed_new_content = preprocess_text(new_content)
            logger.info(f"Preprocessed new content: {preprocessed_new_content}")

            # Regenerate embeddings for the new content
            new_embeddings = embeddings.embed_query(new_content)
            logger.info(f"Embeddings regenerated for new content.")

            # Now update the vector store2 table using pymysql
            conn = connect_db()
            logger.info("Database connection established.")

            with conn.cursor() as cursor:
                # SQL to fetch rows based on LIKE match with the preprocessed content
                sql = """
                    SELECT * 
                    FROM vector_store2
                    WHERE chunk_text LIKE %s
                """
                cursor.execute(sql, (f"%{document.content}%",))
                rows = cursor.fetchall()
                logger.info(
                    f"Found {len(rows)} rows matching the preprocessed new content."
                )

                if not rows:
                    logger.warning(f"No matching rows found for content: {new_content}")
                    return JsonResponse(
                        {
                            "success": False,
                            "message": "No matching rows found to update in vector_store2.",
                        }
                    )

                # If multiple rows are found, choose the best match based on length
                best_match = max(
                    rows, key=lambda row: len(preprocess_text(row["chunk_text"]))
                )
                logger.info(
                    f"Best match found with chunk_text: {best_match['chunk_text']}"
                )

                # Update the vector store2 table using the best match
                update_sql = """
                    UPDATE vector_store2
                    SET chunk_text = %s, embedding = %s
                    WHERE chunk_text = %s
                """
                cursor.execute(
                    update_sql,
                    (new_content, json.dumps(new_embeddings), best_match["chunk_text"]),
                )
                logger.info(
                    f"Vector store updated successfully for chunk_text: {best_match['chunk_text']}"
                )

                # Commit the changes to the database
                conn.commit()
                logger.info("Changes committed to the database.")
                verify_sql = """
                SELECT * FROM vector_store2
                WHERE chunk_text = %s
            """
                cursor.execute(verify_sql, (new_content,))
                updated_row = cursor.fetchone()

                logger.info("==== VECTOR STORE UPDATED SUCCESSFULLY ====")
                logger.info(">> Original content (before update):")
                logger.info(best_match)
                logger.info(">> Updated row from DB (after update):")
                logger.info(updated_row)  # logs the full row dict
                logger.info("===========================================")
                # Close the connection
                conn.close()
                logger.info("Database connection closed.")
                
            document.content = new_content
            document.save()
            logger.info(f"Document content updated successfully.")
            return JsonResponse(
                {
                    "success": True,
                    "message": "Embedding regenerated and stored successfully!",
                }
            )

        except Exception as e:
            logger.error(f"An error occurred: {str(e)}")
            return JsonResponse({"success": False, "error": str(e)})

    logger.warning("Invalid request method.")
    return JsonResponse({"success": False, "error": "Invalid request"})
