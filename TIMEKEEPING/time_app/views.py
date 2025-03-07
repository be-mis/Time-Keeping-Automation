from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
import pandas as pd
from . forms import FileFieldForm
import uuid, os, io, re
import json
from django.core.serializers.json import DjangoJSONEncoder
from . models import TimeCard
from datetime import datetime, timedelta
from openpyxl import Workbook
import xlsxwriter
from .models import TimeCard
from collections import defaultdict
from django.core.paginator import Paginator
import pdfplumber


from datetime import datetime

ALLOWED_FILE_EXTENSIONS = ['pdf','pdfa', 'pdfx','xfdf','fdf','xdp', 'csv', 'xls', 'xlsx', 'xlsm']



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
                store = request.POST.get('store')
                instance = str(uuid.uuid4())

                save_file(files, instance, store)

                # Extract data and then delete raw files
                if store == 'WDS':
                    extract_wds(instance)
                else:
                    extract_data(instance)

                delete_raw(instance)
                return redirect('show-data', instance=instance)
            
    else:
        form = FileFieldForm()

    return render(request, 'home.html', {"form": form})

###########SAVING THE UPLOADED FILES
def save_file(files, instance, store): #Add uploader here if ever needed


    for file in files:
        TimeCard.objects.create(
            raw_file=file,
            store = store,
            instance=instance,  # Save instance with each file for identification
            original_name=file.name  # Retain the original file name
        )

def convert_to_military_time(time_str):
    """Convert time to 24-hour (military) format if needed."""
    if not time_str:  
        return time_str  

    match = re.match(r"(\d{1,2}):(\d{2})", time_str)
    if match:
        hours, minutes = map(int, match.groups())
        if hours < 12:  # Assume AM unless otherwise specified
            hours += 12
        return f"{hours:02d}:{minutes:02d}"
    
    return time_str  # Return unchanged if format is unexpected

def extract_data(instance):
    extracted_data = []
    files = TimeCard.objects.filter(instance=instance)

    print(f"Found {files.count()} files for instance: {instance}")

    if files.exists():
        for file in files:

            print(f"Processing file: {file.raw_file.name}")

            try:
                
                file.raw_file.open("rb")
                file_content = file.raw_file.read()
                file.raw_file.close()
            except Exception as e:
                print(f"Error reading file {file.raw_file.name}: {e}")
                continue

            file_like_object = io.BytesIO(file_content)

            try:
                with pdfplumber.open(file_like_object) as pdf:
                    current_access_id = None
                    current_table = []
                    headers = []

                    for page_num, page in enumerate(pdf.pages, start=1):
                        # print(f"Processing page {page_num} for {file.store}")

                        text = page.extract_text()

                        # Extract Access ID if available
                        if text:
                            access_id_match = re.search(r"(?:ACCESS ID:\s*|Emp #\s*|EMPLOYEE.*?\(|EMPLOYEENUM\s*)\s*(\d+)", text)
                            if access_id_match:
                                new_access_id = access_id_match.group(1)
                                # print(f"Found Access ID: {new_access_id}")

                                if current_access_id and current_table:
                                    # print(f"Saving data for Access ID: {current_access_id}")
                                    extracted_data.append({
                                        "access_id": current_access_id,
                                        "headers": headers,
                                        "table": current_table
                                    })

                                current_access_id = new_access_id
                                current_table = []


                        if file.store == "LEE":
                            text = page.extract_text()
                            print(f"Extracted text from page {page_num}:\n{text}\n")

                            if text:
                                lines = text.split("\n")  # Split text into lines

                                # Process table rows dynamically
                                formatted_table = []

                                for line in lines:
                                    words = line.split()
                                    if len(words) < 2 or not re.match(r"\d{1,2}/\d{1,2}/\d{4}", words[0]):
                                        continue  # Skip invalid lines

                                    # Extract date and valid time entries
                                    date = words[0]
                                    time_entries = [t for t in words[1:] if re.match(r"^\d{1,2}:\d{2}$", t)]  # Only time format

                                    # If only one pair, shift to IN2/OUT2
                                    row_data = {"DATE": date, "IN1": "", "OUT1": "", "IN2": "", "OUT2": "", "IN3": "", "OUT3": "", "HOURS RENDERED": ""}
                                    
                                    if len(time_entries) == 2:
                                        row_data["IN2"] = time_entries[0]
                                        row_data["OUT2"] = time_entries[1]
                                    else:
                                        in_count, out_count = 1, 1
                                        for i, time in enumerate(time_entries):
                                            if i % 2 == 0:  # Even index = IN time
                                                row_data[f"IN{in_count}"] = time[:5]
                                                in_count += 1
                                            else:  # Odd index = OUT time
                                                row_data[f"OUT{out_count}"] = time[:5]
                                                out_count += 1

                                    formatted_table.append(row_data)

                                # Save LEE in RDS format
                                extracted_data.append({
                                    "access_id": current_access_id,
                                    "headers": ["DATE", "IN1", "OUT1", "IN2", "OUT2", "IN3", "OUT3", "HOURS RENDERED"],
                                    "table": formatted_table
                                })

                        else:
                            # Extract using table detection for RDS
                            extracted_table = page.extract_table()

                            if extracted_table:
                                # print(f"Extracted table from page {page_num}: {extracted_table}")

                                if not headers:
                                    headers = extracted_table[0]  # First row contains headers

                                for row in extracted_table[1:]:  # Skip header row
                                    row_data = {}

                                    in_count = 1
                                    out_count = 1

                                    excess_in = ""   # Track excess from IN1p@rm3$AN
                                    excess_out = ""  # Track excess from OUT2
                                    excess_all = []
                                    last_out_col = None

                                    for i, column_name in enumerate(headers):
                                        value = row[i] if i < len(row) and row[i] else ""  # Get value safely

                                        if "IN" in column_name:
                                            if len(value) > 5:
                                                row_data[f"IN{in_count}"] = value[:5]  # Store first 5 characters in IN
                                                excess_in = value[5:].strip()  # Store excess from IN
                                                excess_all.append(excess_in)
                                                print(f"Excess stored from {column_name}: {excess_in}")  # Debug print
                                            else:
                                                row_data[f"IN{in_count}"] = value
                                                excess_in = None  # Reset if no excess
                                            in_count += 1

                                        elif "OUT" in column_name:
                                            if len(value) > 5:
                                                row_data[f"OUT{out_count}"] = value[:5]
                                                excess_out = value[5:].strip()  # Store excess from OUT
                                                excess_all.append(excess_out)
                                                last_out_col = f"OUT{out_count}"
                                                print(f"Excess extracted from {column_name}: {excess_out}")  # Debug print
                                                print(f"all extracted excess {excess_all}")
                                                
                                            else:
                                                row_data[f"OUT{out_count}"] = value
                                                if value:
                                                    last_out_col = f"OUT{out_count}"

                                            out_count += 1  # Move to the next OUT column

                                        else:
                                            row_data[column_name] = value  # Store other columns as is
                                    if last_out_col and excess_all:
                                        row_data[last_out_col] = max(excess_all)
                                        print(f"Assigned max excess {max(excess_all)} to {last_out_col}")  # Debug print

                                    # Append the processed row to the table
                                    current_table.append(row_data)  # ✅ Ensure data is added



                    if current_access_id and current_table:
                        # print(f"Finalizing data for Access ID: {current_access_id}")
                        extracted_data.append({
                            "access_id": current_access_id,
                            "headers": headers,
                            "table": current_table
                        })

            except Exception as e:
                print(f"Error processing PDF file {file.raw_file.name}: {e}")
                continue
            file.extracted_data = extracted_data
            try:
                file.save()
                print(f"Successfully saved extracted data for file: {file.raw_file.name}")
            except Exception as e:
                print(f"Error saving data for file {file.raw_file.name}: {e}")


        print(f"Data extraction complete for instance: {instance}")
    else:
        print("No files found for the provided instance.")
        return HttpResponse("No files found for the provided instance.")

def extract_wds(instance):
    files = TimeCard.objects.filter(instance=instance)

    for file in files:
        file_path = file.raw_file.path  # Get the file path
        df = pd.read_excel(file_path)

        # Ensure column names are correctly formatted
        df.columns = df.columns.str.strip()

        # Convert DATE to string (Fixes Timestamp issue)
        df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce").dt.strftime("%m/%d/%Y")

        # Convert TIME column to string and format as HH:MM
        df["TIME"] = df["TIME"].astype(str).str.zfill(4)
        df["TIME"] = df["TIME"].str[:2] + ":" + df["TIME"].str[2:]

        # Dictionary to store structured data
        timecard_data = defaultdict(lambda: {
            "access_id": "",
            "headers": ["DATE"],
            "table": defaultdict(lambda: {"DATE": "", "HOURS RENDERED": ""})
        })

        for _, row in df.iterrows():
            access_id = str(row["EMPLOYEENUM"]).strip()  # Set access_id
            date = row["DATE"].strip()
            time = row["TIME"].strip()
            log_type = row["LOGTYPE"].strip().upper()

            # Assign access_id to employee data
            timecard_data[access_id]["access_id"] = access_id

            # Create the date entry if not exists
            entry = timecard_data[access_id]["table"][date]
            entry["DATE"] = date  # ✅ Converted to string format

            # Count existing INs and OUTs dynamically
            in_count = sum(1 for key in entry if key.startswith("IN"))
            out_count = sum(1 for key in entry if key.startswith("OUT"))

            # Assign IN/OUT dynamically
            if log_type == "IN":
                entry[f"IN{in_count + 1}"] = time
                if f"IN{in_count + 1}" not in timecard_data[access_id]["headers"]:
                    timecard_data[access_id]["headers"].append(f"IN{in_count + 1}")
            elif log_type == "OUT":
                entry[f"OUT{out_count + 1}"] = time
                if f"OUT{out_count + 1}" not in timecard_data[access_id]["headers"]:
                    timecard_data[access_id]["headers"].append(f"OUT{out_count + 1}")

        # Calculate "HOURS RENDERED" for each day
        for access_id, value in timecard_data.items():
            for entry in value["table"].values():
                hours_rendered = timedelta()
                in_times = sorted([entry[key] for key in entry if key.startswith("IN")])
                out_times = sorted([entry[key] for key in entry if key.startswith("OUT")])

                # Match IN/OUT times dynamically
                for in_time, out_time in zip(in_times, out_times):
                    try:
                        t1 = datetime.strptime(in_time, "%H:%M")
                        t2 = datetime.strptime(out_time, "%H:%M")
                        hours_rendered += t2 - t1
                    except ValueError:
                        continue  # Skip invalid times

                # Format as HH:MM
                total_seconds = max(hours_rendered.total_seconds(), 0)
                formatted_hours = f"{int(total_seconds // 3600):01}:{int((total_seconds % 3600) // 60):02}"
                entry["HOURS RENDERED"] = formatted_hours

            # Convert table to list
            value["table"] = list(value["table"].values())

        # Save extracted data to the TimeCard model as a Python object
        file.extracted_data = list(timecard_data.values())  # Store as a Python object
        file.save()

    print("Extraction completed successfully!")

def delete_raw(instance):
    raw_files = TimeCard.objects.filter(instance=instance)
    for file in raw_files:
        file.raw_file.delete(save=True)  # Delete the file
        print(f"Successfully deleted raw file for instance: {instance}")

def show_data(request, instance):
    files = TimeCard.objects.filter(instance=instance)

    if not files.exists():
        return HttpResponse("No extracted data found.")

    extracted_data = []  # Combined extracted data across all files

    for file in files:
        if isinstance(file.extracted_data, list):
            for entry in file.extracted_data:
                access_id = entry.get("access_id")
                table_data = entry.get("table", [])

                if access_id and table_data:
                    for row in table_data:
                        date = row.get("DATE", "")
                        
                        # Find the first non-empty IN entry
                        first_in = None
                        for i in range(1, 8):  # Adjust the range based on the maximum number of IN entries you expect
                            in_key = f"IN{i}"
                            if row.get(in_key):
                                first_in = row[in_key]
                                break  # Exit the loop once the first IN is found

                        # Find the last non-empty OUT entry
                        last_out = None
                        for i in range(7, 0, -1):  # Start from OUT7 down to OUT1
                            out_key = f"OUT{i}"
                            if row.get(out_key):
                                last_out = row[out_key]
                                break  # Exit the loop once the last OUT is found


                        # Convert last OUT to military time if needed
                        if last_out:
                            match = re.match(r"(\d{1,2}):(\d{2})", last_out)
                            if match:
                                hours, minutes = map(int, match.groups())
                                if hours < 12:
                                    hours += 12
                                last_out = f"{hours}:{minutes:02d}"

                        extracted_data.append({
                            "access_id": access_id,
                            "DATE": date,
                            "IN": first_in,
                            "OUT": last_out
                        })

    # Paginate all extracted data
    page_number = request.GET.get("page", 1)
    paginator = Paginator(extracted_data, 100)  # Show 10 entries per page
    page_obj = paginator.get_page(page_number)

    return render(request, "all_data.html", {
        "page_obj": page_obj,
        "instance": instance,
        "files":files
    })

def download_all(request, instance):
    files = TimeCard.objects.filter(instance=instance)
    for file in files:
        try:
            data = file.extracted_data  

            if isinstance(data, str):  
                # print("Extracted data is a string, parsing JSON...")  # Debug print
                data = json.loads(data)

            # print("Extracted Data:", json.dumps(data, indent=4))  # Debug print

            rows = []
            emp_order = []

            for record in data:
                emp_no = record.get("access_id", "UNKNOWN_ID")
                # print(f"Processing Access ID: {emp_no}")  # Debug print

                if emp_no not in emp_order:
                    emp_order.append(emp_no)

                for entry in record.get("table", []):
                    # print("Raw Entry Data:", entry)  # Debug print

                    # Ensure DATE key exists
                    attend_date = entry.get("DATE") or entry.get("Date") or entry.get("date")
                    if not attend_date:
                        print(f"Skipping entry, no valid DATE key found: {entry}")  # Debug print
                        continue

                    # Select FIRST available IN
                    attend_time_in = None
                    for i in range(1, 8):  # Adjust the range based on the maximum number of IN entries you expect
                        in_key = f"IN{i}"
                        if entry.get(in_key):
                            attend_time_in = entry[in_key]
                            break  # Exit the loop once the first IN is found
                    attend_status_in = 1 if attend_time_in else None  

                    # Select LAST available OUT dynamically
                    attend_time_out = None
                    for i in range(7, 0, -1):  # Start from OUT7 down to OUT1
                        out_key = f"OUT{i}"
                        if entry.get(out_key):
                            attend_time_out = entry[out_key]
                            break  # Exit the loop once the last OUT is found

                    attend_time_out = convert_to_military_time(attend_time_out) if attend_time_out else None
                    attend_status_out = 0 if attend_time_out else None  

                    # Append IN record if available
                    if attend_time_in:
                        rows.append([emp_no, attend_date, attend_time_in, attend_status_in])

                    # Append OUT record if available
                    if attend_time_out:
                        rows.append([emp_no, attend_date, attend_time_out, attend_status_out])

            if not rows:
                # print("No valid attendance data found!")  # Debug print
                return HttpResponse("No attendance data available.", status=400)

            df = pd.DataFrame(rows, columns=["Emp_No", "Attend_Date", "Attend_Time", "Attend_Status"])
            # print("Generated DataFrame:")  
            # print(df)  # Debug print

            # Ensure Emp_No order is preserved, and INs (1s) come before OUTs (0s) for each Emp_No
            df["Emp_No"] = pd.Categorical(df["Emp_No"], categories=emp_order, ordered=True)
            df.sort_values(by=["Emp_No", "Attend_Status", "Attend_Date"], ascending=[True, False, True], inplace=True)

            response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="Template for {file.original_name}.xlsx"'

            with pd.ExcelWriter(response, engine="xlsxwriter") as writer:
                df.to_excel(writer, index=False)

            # print("Excel file successfully generated!")  # Debug print
            return response

        except TimeCard.DoesNotExist:
            # print(f"Error: TimeCard ID {pk} not found!")  # Debug print
            return HttpResponse("File not found", status=404)

        except Exception as e:
            # print(f"Unexpected error: {e}")  # Debug print
            return HttpResponse(f"Error: {str(e)}", status=500)































