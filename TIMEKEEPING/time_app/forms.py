from django import forms
from . models import TimeCard

class TimeCardFormInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class TimeCardForm(forms.FileField): #FileField
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", TimeCardFormInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        # Handle single or multiple files
        cleaned_files = []
        if isinstance(data, (list, tuple)):  # Handle multiple files
            for file_data in data:
                cleaned_files.append(super().clean(file_data, initial))
        else:  # Handle single file
            cleaned_files.append(super().clean(data, initial))
        return cleaned_files
    
    
class FileFieldForm(forms.Form):
    file_field = TimeCardForm()




