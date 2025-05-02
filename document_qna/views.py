import os
import json
import time
import pymysql
import re
import pickle
from datetime import datetime
from django.utils.dateparse import parse_datetime
from django.utils.timezone import make_aware
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
from django.utils.timezone import now
from django.db import transaction
from rest_framework import status
from rest_framework import serializers
import logging

# Setup logging
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, 'backend.log'),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)
## Initialization
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
llm = ChatGroq(groq_api_key=GROQ_API_KEY, model="llama-3.3-70b-versatile")

VECTOR_STORE = None
FAISS_INDEX_PATH = "faiss_index.pkl"

MYSQL_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "port": int(os.getenv("DB_PORT", 3306)),
}
## Hugging face embedding model to convert text chunks into numerical vectors.
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

standard_response_prompt = ChatPromptTemplate.from_template(
    """
    You are an AI assistant specialized in answering customer queries.  
    Provide accurate, in-depth, and concise responses strictly based on the given context and Chat History.  

    **Guidelines:**  
    - Your response must be **precise and to the point**.  
    - **Do not** use phrases like "According to the provided context."  
    - **Never** disclose API keys, passwords, or personal user information.  
    - **If the answer is not in the context, respond with:** "Not enough info to answer."  
    - If the user requests a response in another language, provide **both English and the requested language**.
    - **Always start your answer in requested language followed by English** with **"In English:"**, if applicable. 
     
     
    
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
    You are an AI assistant that is specialized in answering customer queries.  
    The previous response did not meet user expectations, so generate a **new, fresh, and factually correct** answer.  

    **Guidelines:**  
    - Ensure the new answer **differs from the previous response** but still remains accurate.  
    - Avoid repetition of the previous answer.  
    - Maintain clarity, conciseness, and factual correctness.  
    - If the user requests a response in another language, provide **both English and the requested language**.
    - **Always start your answer in requested language followed by English** with **"In English:"**, if applicable. 
     

    **Chat History:**  
    {chat_history}

    **<context>**
    {context}
    **</context>**

    **User Question:** {question}
    """
)

latest_query = ""
## Memory concept :
CHAT_HISTORY_LIMIT = 10
memory = ConversationBufferWindowMemory(
    memory_key="chat_history",
    output_key="answer",
    k=CHAT_HISTORY_LIMIT,
    return_messages=True,
)


def get_chat_history(memory):
    memory.load_memory_variables({}).get("chat_history", [])


def clear_chat_history():
    """Clear chat memory."""
    global memory
    memory.clear()
    print("Chat history cleared.")


## Database operations:
def connect_db():
    return pymysql.connect(**MYSQL_CONFIG, cursorclass=pymysql.cursors.DictCursor)


## Storing embedings into DB.
def store_vector_in_db(file_name, chunk_text, embedding):
    logger.info(f"Storing vector for file: {file_name}")
    conn = connect_db()
    try:
        with conn.cursor() as cursor:
            sql = "INSERT INTO vector_store (file_name, chunk_text, embedding) VALUES (%s, %s, %s)"
            cursor.execute(sql, (file_name, chunk_text, json.dumps(embedding)))
        conn.commit()
        logger.info(f"Vector stored successfully for chunk (length: {len(chunk_text)}) from {file_name}")
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
            cursor.execute("SELECT chunk_text, embedding FROM vector_store LIMIT 10000")
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


## Extracting information from JSON Data.
def extract_json_text_as_chunks(data):
    """Convert each JSON object into a single chunk."""
    if isinstance(data, list):
        return [json.dumps(item, ensure_ascii=False) for item in data]
    elif isinstance(data, dict):
        return [json.dumps(data, ensure_ascii=False)]
    print("Unexpected JSON format: Root element is not a dict or list.")
    return []


## This function triggers when user uploads new document.
def process_new_document(file_path):
    try:
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            return

        text_entries = []

        if file_path.endswith(".pdf"):
            loader = PyPDFLoader(file_path)
            documents = loader.load()  # List of Document objects

            # Extract text content from each Document object
            raw_texts = [doc.page_content for doc in documents]

            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000, chunk_overlap=200
            )
            text_entries = text_splitter.split_text("\n".join(raw_texts))
        elif file_path.endswith(".csv"):
            text_entries = [
                doc.page_content for doc in CSVLoader(file_path=file_path).load()
            ]
        elif file_path.endswith(".json"):
            with open(file_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
            if json_data:
                text_entries = extract_json_text_as_chunks(json_data)
            else:
                print(f"JSON file is empty: {file_path}")
                return
        else:
            print(f"Unsupported file format: {file_path}")
            return

        if not text_entries:
            print(f"No valid text extracted from: {file_path}")
            return

        for text in text_entries:
            embedding = embeddings.embed_query(text)
            store_vector_in_db(os.path.basename(file_path), text, embedding)

        load_vectors_from_db()
        ## Remove file from Media folder.
        os.remove(file_path)
        print(f"File processed and deleted: {file_path}")
        return True
    except Exception as e:
        print(f"Error processing document ({file_path}): {e}")
        return False


def save_unanswered_question(question):
    """Store unanswered questions in the database."""
    if not UnansweredQuestion.objects.filter(question=question).exists():
        UnansweredQuestion.objects.create(question=question)


def get_unanswered_questions():
    return UnansweredQuestion.objects.all()


def get_retrieval_chain(regenerate=False):
    """Creates or retrieves a cached retrieval chain with the appropriate prompt."""

    if not load_index_or_db():
        return None

    retriever = None
    if retriever is None:
        retriever = VECTOR_STORE.as_retriever(search_kwargs={"k": 5})
        cache.set("retriever", retriever, timeout=3600)

    ## prompt selection
    selected_prompt = (
        regenerate_response_prompt if regenerate else standard_response_prompt
    )

    retrieval_chain_instance = ConversationalRetrievalChain.from_llm(
        llm=llm,
        retriever=retriever,
        combine_docs_chain_kwargs={"prompt": selected_prompt},
        memory=memory,
        return_source_documents=True,
    )
    return retrieval_chain_instance


def store_retrieved_documents(q_id, response, save_results=False):
    """
    Stores retrieved documents separately in the database, ensuring unique query IDs.
    Supports multiple users operating simultaneously while preventing query_id conflicts.
    """
    if not save_results:
        return None

    folder_name = "media/retrieved_docs"
    os.makedirs(folder_name, exist_ok=True)

    with transaction.atomic():  # Ensure atomic transaction
        existing_doc = (
            SourceDocument.objects.select_for_update().filter(q_id_id=q_id).first()
        )

        if existing_doc:
            query_id = existing_doc.query_id  # Reuse existing query_id if found
        else:
            query_id = str(uuid.uuid4())  # Generate new query_id if not found

        for i, content in enumerate(response.get("source_documents", []), start=1):

            source_type = "unknown"
            extracted_title = f"Document_{i}"
            dynamic_url = ""

            try:
                doc_data = json.loads(content.page_content)

                if "id" in doc_data:
                    source_type = "article"
                    dynamic_url = f"https://manage.accuwebhosting.com/knowledgebase/{doc_data['id']}"
                elif "tid" in doc_data:
                    source_type = "ticket"
                    dynamic_url = f"https://manage.accuwebhosting.com/admin20112012/supporttickets.php?action=viewticket&id={doc_data['tid']}"
                elif "internal_id" in doc_data:
                    source_type = "internal_wiki"
                    dynamic_url = f"https://support.jilesh.com/internal_wiki/{doc_data['internal_id']}"
                elif "website_url" in doc_data:
                    source_type = "website"
                    dynamic_url = doc_data["website_url"]

                extracted_title = (
                    doc_data.get("title")
                    or doc_data.get("question")
                    or doc_data.get("query")
                    or extracted_title
                )

            except Exception as e:
                print(f"Error parsing document content: {e}")

            # Save document under the locked query_id
            SourceDocument.objects.create(
                query_id=query_id,
                title=extracted_title,
                file_path=dynamic_url,
                content=content.page_content,
                q_id_id=q_id,
                source=source_type,
                # llm_answer = formatted_answer,
            )

            print(
                f"(Title: {extracted_title}, Source Type: {source_type}, URL: {dynamic_url})"
            )

    return query_id


def view_source_document(query_id):
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


def view_source_document_by_query(query):
    """
    Fetches all source documents for a given query and returns them as a list.
    """
    documents = SourceDocument.objects.filter(query=query)

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
    user_query, TMS_agent="Ronak", chat_id="67ecd2dd-c36c-800f-89fc-45b88a4a032e"
):
    """Handles the query processing logic and generates bilingual responses."""
    retrieval_chain = get_retrieval_chain(regenerate=False)
    if retrieval_chain is None:
        return JsonResponse(
            {"error": "No indexed documents found. Please upload some files first."},
            status=400,
        )
    # load_vectors_from_db()

    start_time = time.process_time()
    response = retrieval_chain.invoke({"question": user_query})
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
    # Split the response into English and requested language
    formatted_answer = format_response(answer)
    answer_in_requested_language = ""
    answer_in_english = formatted_answer

    if "In English:" in formatted_answer:
        parts = formatted_answer.split("In English:")
        answer_in_requested_language = parts[0].strip()
        answer_in_english = parts[1].strip() if len(parts) > 1 else ""

    query_history = QueryHistory.objects.create(
        query=user_query,
        TMS_agent=TMS_agent,
        chat_id=chat_id,
        llm_answer=answer_in_english,
    )
    q_id = query_history.id
    query_id = store_retrieved_documents(q_id, response, True)

    return JsonResponse(
        {
            "answer_in_english": formatted_answer,
            "answer_in_requested_language": answer_in_requested_language,
            "response_time": response_time,
            "query_id": query_id,
        },
        safe=False,
    )


def regenerate_response(user_query):
    """Handles regenerating the response based on the previous answer."""
    retrieval_chain = get_retrieval_chain(regenerate=True)
    if retrieval_chain is None:
        return {
            "error": "No indexed documents found. Please upload some files first."
        }, 400

    start_time = time.process_time()
    response = retrieval_chain.invoke(
        {
            "question": user_query,
        }
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
            "Not enough info to answer.",
        ]
    ):
        save_unanswered_question(user_query)

    query_id = store_retrieved_documents(user_query, response, True)
    formatted_answer = format_response(answer)

    answer_in_requested_language = formatted_answer
    answer_in_english = ""

    if "In English:" in formatted_answer:
        parts = formatted_answer.split("In English:")
        answer_in_requested_language = parts[0].strip()
        answer_in_english = parts[1].strip() if len(parts) > 1 else ""

    return JsonResponse(
        {
            "answer_in_english": answer_in_english,
            "answer_in_requested_language": answer_in_requested_language,
            "response_time": response_time,
            "query_id": query_id,
        },
        safe=False,
    )


def format_response(response):
    """Formats the bot's response beautifully with better readability."""
    # Convert markdown-like formatting to HTML-friendly format
    response = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", response)
    response = re.sub(r"\*(.*?)\*", r"<i>\1</i>", response)

    response = re.sub(r"(\n?[-•*]\s+)", r"<br>\1", response)
    response = re.sub(r"(\n?\d+\.\s+)", r"<br>\1", response)

    response = re.sub(r"\n{2,}", r"<br><br>", response)

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

def login_view(request):
    if request.session.get("authenticated"):
        return redirect("home")

    error = None

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        try:
            conn = connect_db()
            with conn.cursor() as cursor:
                query = "SELECT * FROM user_credentials WHERE username = %s AND password = %s"
                cursor.execute(query, (username, password))
                user = cursor.fetchone()
        finally:
            conn.close()

        if user:
            request.session["authenticated"] = True
            request.session["username"] = username
            return redirect("home")
        else:
            error = "Invalid credentials!"

    return render(request, "login.html", {"error": error})

def logout_view(request):
    request.session.flush()
    return redirect('login')

# ## This is the Index view of Django.
def home(request):
    # create_dummy_api_token()
    if not request.session.get("authenticated"):
        return redirect("login")
    if request.method == "POST":
        ## Document Upload section.
        if "document" in request.FILES:
            file = request.FILES["document"]
            file_path = default_storage.save(f"media/{file.name}", file)
            if process_new_document(file_path):
                return render(request, "index.html", {"message": "Upload successful!"})
            return render(request, "index.html", {"error": "Upload failed!"})

        elif "fetch" in request.POST and "query" in request.POST:
            return view_source_document_by_query(query=request.POST["query"])

        elif "query" in request.POST:
            return get_response(request.POST["query"])

    return render(request, "index.html",{ "username": request.session.get("username")})


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


@csrf_exempt
@api_view(["POST"])
def query_api(request):
    """REST API to validate token and process queries."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return Response({"error": "Invalid JSON format"}, status=400)

    token = data.get("token") or request.headers.get("Authorization")
    user_query = data.get("query")
    username = data.get("username")
    chat_id = data.get("chat_id")
    regenerate_flag = False

    if not token or not user_query:
        return Response({"error": "Token and query are required."}, status=400)

    user = get_user_from_token(token)
    if not user:
        return Response({"error": "Invalid or expired token."}, status=403)

    if user_query in IGNORED_QUERIES:
        return Response(
            {"message": f"Hello {user.username}, how can I assist you today?"},
            status=200,
        )

    if regenerate_flag:
        return regenerate_response(user_query)
    else:
        return get_response(user_query)


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

    data = json.loads(request.body)
    flag = data.get("flag")
    query_id = data.get("query_id")
    answer_by_team = data.get("answer_by_team", "").strip()

    if flag is not True:
        return Response(
            {"error": "Flag must be True"}, status=status.HTTP_400_BAD_REQUEST
        )

    token = data.get("token") or request.headers.get("Authorization")
    user = get_user_from_token(token)

    if not token or not query_id:
        return Response({"error": "Token and query_id are required."}, status=400)

    if not user:
        return Response({"error": "Invalid or expired token."}, status=403)

    if not answer_by_team:
        return Response(
            {"error": "answer_by_team cannot be empty"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        updated_rows = SourceDocument.objects.filter(query_id=query_id).update(
            relevant=False, answer_by_team=answer_by_team
        )

        if updated_rows == 0:
            return Response(
                {"error": "Query ID not found"}, status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            {"message": "Successfully updated the document"}, status=status.HTTP_200_OK
        )

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
    queries = QueryHistory.objects.filter(chat_id=chat_id).only("id")

    if queries.count() == 0:
        return render(
            request, "chat_details.html", {"error": "No queries found for this chat ID"}
        )

    # Get all irrelevant documents linked to these queries
    irrelevant_docs = SourceDocument.objects.filter(
        q_id__in=queries, relevant=False
    ).select_related("q_id")

    context = {
        "chat_id": chat_id,
        "queries": queries,
        "irrelevant_documents": irrelevant_docs,
    }

    return render(request, "chat_details.html", context)


@api_view(["GET"])
def list_chat_ids(request):
    from_date_str = request.GET.get("from_date")
    to_date_str = request.GET.get("to_date")
    print(from_date_str)
    print(to_date_str)
    try:
        if from_date_str:
            from_date = make_aware(datetime.strptime(from_date_str, "%Y-%m-%dT%H:%M"))

        else:
            from_date = make_aware(
                datetime.combine(datetime.today(), datetime.min.time())
            )

        if to_date_str:
            to_date = make_aware(datetime.strptime(to_date_str, "%Y-%m-%dT%H:%M"))
        else:
            to_date = make_aware(
                datetime.combine(datetime.today(), datetime.max.time())
            )
    except ValueError:
        return Response(
            {"error": "Invalid date format. Use YYYY-MM-DDTHH:MM"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    chat_ids = (
        QueryHistory.objects.filter(timestamp__gte=from_date, timestamp__lte=to_date)
        .values("chat_id")
        .distinct()
    )
    print(list(chat_ids))
    return Response(list(chat_ids), status=status.HTTP_200_OK)


def chat_ids(request):
    if not request.session.get('authenticated'):
        return redirect('login')
    return render(request, "chat_id_list.html")


@api_view(["POST"])
def update_query_status(request):
    query_id = request.data.get("query_id")
    new_status = request.data.get("status")
    remarks = request.data.get("remarks", "")

    if not query_id or not new_status:
        return Response(
            {"error": "Query ID and Status are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    query = get_object_or_404(QueryHistory, id=query_id)
    query.status = new_status
    query.remarks = remarks
    query.save()

    return Response(
        {"message": "Query updated successfully"}, status=status.HTTP_200_OK
    )
