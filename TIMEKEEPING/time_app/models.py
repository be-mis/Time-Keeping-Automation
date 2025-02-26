from django.db import models

class TimeCard(models.Model):
    raw_file = models.FileField(upload_to='uploads/')  # Store the uploaded file
    instance = models.CharField(max_length=255)  # Instance ID to track the file's group
    date_of_generation = models.DateTimeField(auto_now_add=True)
    original_name = models.CharField(max_length=255)  # Original file name
    extracted_data = models.JSONField(null=True)
    uploader = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.original_name} - {self.instance}"

    class Meta:
        ordering = ['-date_of_generation']
