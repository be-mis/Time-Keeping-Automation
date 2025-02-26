from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_file, name="home-page"),
    path('data/<str:instance>/', views.show_data, name="show-data"),
    
    path('extract/<str:instance>/', views.extract_data, name="extract-data"),  # Added instance parameter

    path('download/<int:pk>/', views.download_all, name="download-excel")
]

