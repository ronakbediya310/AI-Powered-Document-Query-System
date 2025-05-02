from django.contrib import admin
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static
from . import views
from .views import query_api_sales,flag_relevance,list_chat_ids,fetch_documents_by_query_id,update_query_status,search_chat_by_id,update_query_history_flags,query_api_whmcs,regenerate_embedding

urlpatterns = [
    #  path('', views.index, name='index'), 
    path('', views.index, name='index'),
    path("api/query/sales", query_api_sales, name="query_api_sales"),
    path("api/query/whmcs", query_api_whmcs, name="query_api_whmcs"),
    path("api/query/flag", flag_relevance, name="query_api_flag"),
    path("api/fetch_by_id", fetch_documents_by_query_id, name="fetch_documents_by_query_id"),
    path("api/flag", flag_relevance, name="flag_relevance"),
    path("list/",views.chat_ids, name = "chat_ids"),
    path('api/list-chat-ids/sales/', list_chat_ids, name='list_chat_ids'),
    path('chat-details/sales/<chat_id>', views.chat_details_view, name='chat_details'),
    path("api/update-query-status/", update_query_status, name="update-query-status"),
    path('api/search-chat-id/', search_chat_by_id, name='search_chat_by_id'),
    path('api/update-query-history/', update_query_history_flags, name='update_query_history'),
    path('api/regenerate-embedding/',regenerate_embedding, name='regenerate_embeddings'),
    path("api/store-team-answer", views.store_team_answer, name="store_team_answer"),
    path('api/unresolved-queries/', views.show_unresolved_queries, name='unresolved_queries'),
    path('api/high-proxy-report',views.high_proxy_report,name='high_proxy_report')
]