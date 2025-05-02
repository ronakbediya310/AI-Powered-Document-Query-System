from django.urls import path
from . import views
from .views import query_api,fetch_documents_by_query_id,flag_relevance
from .views import list_chat_ids
from django.conf import settings
from django.conf.urls.static import static
from .views import update_query_status

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('', views.home, name='home'), 
    path('index/', views.home, name='home'),
    path("api/query", query_api, name="query_api"),
    path("api/fetch_by_id", fetch_documents_by_query_id, name="fetch_documents_by_query_id"),
    path("api/flag", flag_relevance, name="flag_relevance"),
    path("list/",views.chat_ids, name = "chat_ids"),
    path('api/list-chat-ids/', list_chat_ids, name='list_chat_ids'),
    path('chat-details/<uuid:chat_id>/', views.chat_details_view, name='chat_details'),
    path("api/update-query-status/", update_query_status, name="update-query-status"),
    path('logout/', views.logout_view, name='logout'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
