from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
import pandas as pd
from . forms import FileFieldForm
import uuid, os, io, re
import json
from . models import TimeCard
from openpyxl import Workbook
import xlsxwriter
from .models import TimeCard
from collections import defaultdict
from django.core.paginator import Paginator
import pdfplumber


from datetime import datetime

ALLOWED_FILE_EXTENSIONS = ['pdf','pdfa', 'pdfx','xfdf','fdf','xdp']



# Create your views here.
@csrf_exempt
def upload_file(request):
    if request.method == 'POST':
        form = FileFieldForm(request.POST, request.FILES)
        if form.is_valid():          
            files = request.FILES.getlist('file_field')

            for file in files:
                file_extension = os.path.splitext(file.name)[1][1:].lower()
                if file_extension not in ALLOWED_FILE_EXTENSIONS:
                    print(f"File type '{file_extension}' is not allowed. Only .pdf files are accepted.")
                    continue

            instance = str(uuid.uuid4())
            save_file(files, instance)

            # Extract data and then delete raw files
            extract_data(instance)  

            return redirect('show-data', instance=instance)
            
    else:
        form = FileFieldForm()

    return render(request, 'home.html', {"form": form})



###########SAVING THE UPLOADED FILES
def save_file(files, instance): #Add uploader here if ever needed
    for file in files:
        TimeCard.objects.create(
            raw_file=file,
            instance=instance,  # Save instance with each file for identification
            original_name=file.name  # Retain the original file name
        )


def extract_data(instance):
    extracted_data = []
    files = TimeCard.objects.filter(instance=instance)

    if files.exists():
        for file in files:
            file.raw_file.open("rb")  # Open file in binary mode
            file_content = file.raw_file.read()
            file.raw_file.close()  # Close after reading
            file_like_object = io.BytesIO(file_content)

            with pdfplumber.open(file_like_object) as pdf:
                current_access_id = None  # Track Access ID per page
                current_table = []  # Store table rows for the current Access ID

                for page in pdf.pages:
                    text = page.extract_text()

                    if text:
                        # Extract Access ID from header (store separately)
                        access_id_match = re.search(r"ACCESS ID:\s*(\d+)", text)
                        if access_id_match:
                            new_access_id = access_id_match.group(1)

                            # If Access ID changes, save the previous table
                            if current_access_id and current_table:
                                extracted_data.append({
                                    "access_id": current_access_id,
                                    "table": current_table
                                })

                            # Start new table for the new Access ID
                            current_access_id = new_access_id
                            current_table = []

                    # Extract table using pdfplumber's table detection
                    extracted_table = page.extract_table()

                    if extracted_table:
                        for row in extracted_table[1:]:
                            if len(row) < 8:
                                # Ensure exactly 8 elements per row, replacing None with ""
                                row = [(col if col is not None else "") for col in row]
                                row += [""] * (8 - len(row))  # Fill missing columns

                            current_table.append({
                                "date": row[0],
                                "in1": row[1],
                                "out1": row[2],
                                "in2": row[3],
                                "out2": row[4],
                                "in3": row[5],
                                "out3": row[6],
                                "hoursrendered": row[7],
                            })

                # Save last table after exiting loop
                if current_access_id and current_table:
                    extracted_data.append({
                        "access_id": current_access_id,
                        "table": current_table
                    })

            # Save extracted data to the model
            file.extracted_data = extracted_data
            file.save()

        # Now delete raw files after extraction
        delete_raw(instance)
        print(f"Data extracted and saved for instance: {instance}")
    else:
        return HttpResponse("No files found for the provided instance.")



def delete_raw(instance):
    raw_files = TimeCard.objects.filter(instance=instance)
    for file in raw_files:
        file.raw_file.delete(save=True)  # Delete the file
        print(f"Successfully deleted raw file for instance: {instance}")



def is_valid_entry(entry):
    """Checks if an entry has a valid date and expected in/out time fields."""
    try:
        # Validate date format
        datetime.strptime(entry["date"], "%m/%d/%Y")

        # Ensure at least one valid 'in' and 'out' time exists
        valid_keys = {"in1", "out1", "in2", "out2", "in3", "out3"}
        return any(key in entry and entry[key] for key in valid_keys)
    except (ValueError, KeyError):
        return False  # Invalid date format or missing key

def show_data(request, instance):
    files = TimeCard.objects.filter(instance=instance)
    
    if not files.exists():
        return HttpResponse("No extracted data found.")

    extracted_data = []
    file_id_map = {}  # Store access_id -> file_id mapping

    for file in files:
        if isinstance(file.extracted_data, list):  # Ensure it's a list
            for entry in file.extracted_data:
                extracted_data.append(entry)
                file_id_map[entry["access_id"]] = file.id  # Map access_id to file.id

    # Pagination logic
    page_number = request.GET.get("page", 1)  # Get page number from URL query
    paginator = Paginator(extracted_data, 1)  # Show 1 Access ID per page
    page_obj = paginator.get_page(page_number)

    # Pass file_id of the first object in the current page
    file_id = file_id_map.get(page_obj.object_list[0]["access_id"], None) if page_obj.object_list else None

    return render(request, "all_data.html", {"page_obj": page_obj, "file_id": file_id})


def download_all(request, pk):
    try:
        # Retrieve the file object by its ID
        file = TimeCard.objects.get(id=pk)
        data = file.extracted_data  # Assuming extracted_data is stored as JSON

        # Ensure extracted_data is properly loaded
        if isinstance(data, str):  
            data = json.loads(data)

        rows = []
        emp_order = []  # To preserve original access ID order

        # Process the extracted JSON data
        for record in data:
            emp_no = record["access_id"]
            if emp_no not in emp_order:
                emp_order.append(emp_no)  # Store original order of access IDs

            for entry in record["table"]:
                attend_date = entry["date"]
                attend_time_in = entry.get("in1") or entry.get("in2") or entry.get("in3")  # Prioritize in1
                attend_status_in = 1 if attend_time_in else 0

                # Prioritize last out value (out3 > out2 > out1)
                attend_time_out = entry.get("out3") or entry.get("out2") or entry.get("out1")
                attend_status_out = 0 if attend_time_out else 1  # Mark out as 0

                # Append in1 row
                if attend_time_in:
                    rows.append([emp_no, attend_date, attend_time_in, attend_status_in])

                # Append last out row
                if attend_time_out:
                    rows.append([emp_no, attend_date, attend_time_out, attend_status_out])

        # Convert data to Pandas DataFrame
        df = pd.DataFrame(rows, columns=["Emp_No", "Attend_Date", "Attend_Time", "Attend_Status"])

        # Preserve access_id order while sorting Attend_Status per access_id
        df["Emp_No"] = pd.Categorical(df["Emp_No"], categories=emp_order, ordered=True)
        df.sort_values(by=["Emp_No", "Attend_Status"], ascending=[True, False], inplace=True)

        # Generate Excel file response
        response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="attendance_report.xlsx"'

        # Save DataFrame to Excel
        with pd.ExcelWriter(response, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False)

        return response

    except TimeCard.DoesNotExist:
        return HttpResponse("File not found", status=404)

    except Exception as e:
        return HttpResponse(f"Error: {str(e)}", status=500)