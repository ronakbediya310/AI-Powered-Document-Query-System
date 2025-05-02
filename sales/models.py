from django.db import models
from django.utils.timezone import now
import uuid

# Create your models here.

class QueryHistory(models.Model):
    id = models.AutoField(primary_key=True)
    chat_id = models.CharField(max_length=255, editable=False, db_index=True)
    chat_link = models.URLField(max_length=500,default="N/A")
    TMS_agent = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True) 
    query = models.CharField(max_length=255, default="sample")  
    llm_answer = models.TextField(null=True, blank=True)  
    answer_by_team = models.TextField(null=True, blank=True)   
    remarks = models.TextField(null=True, blank=True)
    is_flagged = models.BooleanField(default=False)
    is_used = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    is_answer_starts_with_sorry = models.BooleanField(default=False)
    answer_proximity = models.DecimalField(
        max_digits=5,  
        decimal_places=2,
        null=True,
        blank=True,
        
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("Unresolved", "Unresolved"),
            ("Resolved", "Resolved"),
            ("No Action Required", "No Action Required"),
            ("In Progress", "In Progress"),
        ],
        default="No Action Required",
    )
    
    
    class Meta:
        app_label = 'sales' 
        db_table = "sales_queryhistory"
        indexes = [models.Index(fields=['chat_id'],name="sales_chat_id_idx")]  
    
    def __str__(self):
        return f"Chat {self.chat_id} by {self.TMS_agent}"
    
class SourceDocument(models.Model):
    id = models.AutoField(primary_key=True)
    query_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    q_id = models.ForeignKey(QueryHistory,on_delete=models.CASCADE, to_field='id', related_name='source_documents')
    file_path = models.CharField(max_length=500, null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True, null=True)
    content = models.TextField()    
    source = models.CharField(max_length=255, null=True)
    title = models.CharField(max_length=255, null=True)
    relevant = models.BooleanField(default=True)
    
    
  
    class Meta:
        app_label = 'sales' 
        db_table = "sales_sourcedocument"

    
# class SourceDocument(models.Model):
#     id = models.AutoField(primary_key=True)
#     query_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
#     query = models.CharField(max_length=255, default="sample")
#     llm_answer = models.TextField( null=True, blank=True)
#     file_path = models.CharField(max_length=500, null=True, blank=True)
#     content = models.TextField()

#     source = models.CharField(max_length=255, null=True)
#     title = models.CharField(max_length=255, null=True)
#     relevant = models.BooleanField(default=True)

#     class Meta:
#         db_table = "document_qna_sourcedocument"

#     def __str__(self):
#         return f"{self.query} ({self.query_id})"


class UnansweredQuestion(models.Model):
    id = models.AutoField(primary_key=True)  # Ensure primary key is defined
    question = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'sales' 
        db_table = "sales_unansweredquestion"


class AdminUser(models.Model):
    """Model for Admin Users."""

    id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=150, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.username


class APIToken(models.Model):
    """Model for API Tokens."""

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(AdminUser, on_delete=models.CASCADE)
    token = models.CharField(max_length=255, unique=True, default=uuid.uuid4)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        """Check if the token is still valid."""
        return self.expires_at > now()

    def __str__(self):
        return f"Token for {self.user.username}"
