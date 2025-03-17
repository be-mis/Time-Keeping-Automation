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
                elif store == 'EVER':
                    extract_ever(instance)
                elif store == 'FISHER':
                    extract_fisher(instance)
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
    files = TimeCard.objects.filter(instance=instance)

    print(f"Found {files.count()} files for instance: {instance}")

    if files.exists():
        for file in files:
            extracted_data = []  # ✅ Reset extracted_data for each file

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
                        text = page.extract_text()

                        # Extract Access ID
                        if text:
                            access_id_match = re.search(r"(?:ACCESS ID:\s*|Emp #\s*|EMPLOYEE.*?\(|EMPLOYEENUM\s*)\s*(\d+)", text)
                            if access_id_match:
                                new_access_id = access_id_match.group(1)

                                if current_access_id and current_table:
                                    extracted_data.append({
                                        "access_id": current_access_id,
                                        "headers": headers,
                                        "table": current_table
                                    })

                                current_access_id = new_access_id
                                current_table = []

                        # Processing "LEE" store
                        if file.store == "LEE":
                            text = page.extract_text()
                            print(f"Extracted text from page {page_num}:\n{text}\n")

                            if text:
                                lines = text.split("\n")
                                formatted_table = []

                                for line in lines:
                                    words = line.split()
                                    if len(words) < 2 or not re.match(r"\d{1,2}/\d{1,2}/\d{4}", words[0]):
                                        continue

                                    date = words[0]
                                    time_entries = [t for t in words[1:] if re.match(r"^\d{1,2}:\d{2}$", t)]

                                    row_data = {"DATE": date, "IN1": "", "OUT1": "", "IN2": "", "OUT2": "", "IN3": "", "OUT3": "", "HOURS RENDERED": ""}
                                    
                                    if len(time_entries) == 2:
                                        row_data["IN2"] = time_entries[0]
                                        row_data["OUT2"] = time_entries[1]
                                    else:
                                        in_count, out_count = 1, 1
                                        for i, time in enumerate(time_entries):
                                            if i % 2 == 0:
                                                row_data[f"IN{in_count}"] = time[:5]
                                                in_count += 1
                                            else:
                                                row_data[f"OUT{out_count}"] = time[:5]
                                                out_count += 1

                                    formatted_table.append(row_data)

                                extracted_data.append({
                                    "access_id": current_access_id,
                                    "headers": ["DATE", "IN1", "OUT1", "IN2", "OUT2", "IN3", "OUT3", "HOURS RENDERED"],
                                    "table": formatted_table
                                })

                        # Processing "RDS" store
                        else:
                            extracted_table = page.extract_table()

                            if extracted_table:
                                if not headers:
                                    headers = extracted_table[0]

                                for row in extracted_table[1:]:
                                    row_data = {}
                                    in_count = 1
                                    out_count = 1

                                    excess_in = ""
                                    excess_out = ""
                                    excess_all = []
                                    last_out_col = None

                                    for i, column_name in enumerate(headers):
                                        value = row[i] if i < len(row) and row[i] else ""

                                        if "IN" in column_name:
                                            if len(value) > 5:
                                                row_data[f"IN{in_count}"] = value[:5]
                                                excess_in = value[5:].strip()
                                                excess_all.append(excess_in)
                                            else:
                                                row_data[f"IN{in_count}"] = value
                                                excess_in = None
                                            in_count += 1

                                        elif "OUT" in column_name:
                                            if len(value) > 5:
                                                row_data[f"OUT{out_count}"] = value[:5]
                                                excess_out = value[5:].strip()
                                                excess_all.append(excess_out)
                                                last_out_col = f"OUT{out_count}"
                                            else:
                                                row_data[f"OUT{out_count}"] = value
                                                if value:
                                                    last_out_col = f"OUT{out_count}"
                                            out_count += 1

                                        else:
                                            row_data[column_name] = value

                                    if last_out_col and excess_all:
                                        row_data[last_out_col] = max(excess_all)

                                    current_table.append(row_data)

                    if current_access_id and current_table:
                        extracted_data.append({
                            "access_id": current_access_id,
                            "headers": headers,
                            "table": current_table
                        })

            except Exception as e:
                print(f"Error processing PDF file {file.raw_file.name}: {e}")
                continue

            # ✅ Save only the data for the current file
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


def normalize_keys(entry):
    """Standardizes keys to uppercase and removes spaces in keys like 'In x' and 'Out x'."""
    normalized_entry = {}
    for key, value in entry.items():
        normalized_key = re.sub(r'\s+', '', key.strip().upper())  # Normalize spaces and case
        normalized_entry[normalized_key] = value
    return normalized_entry

def show_data(request, instance):
    files = TimeCard.objects.filter(instance=instance).only("extracted_data", "original_name")  # Optimize query
    has_data = 0
    if not files.exists():
        return HttpResponse("No extracted data found.")

    extracted_data = []  # Clear data at the start

    for file in files:
        file_data = []  # Separate list for each file
        if isinstance(file.extracted_data, list):
            for entry in file.extracted_data:
                access_id = entry.get("access_id")
                table_data = entry.get("table", [])

                if access_id and table_data:
                    for row in table_data:
                        row = normalize_keys(row)  # Standardize keys dynamically
                        date = row.get("DATE", "")

                        # Find the first non-empty IN entry dynamically
                        first_in = next((row[key] for i in range(1, 8) if (key := f"IN{i}") in row and row[key]), None)

                        # Find the last valid non-empty OUT entry dynamically (excluding "0:00" or "00:00")
                        last_out = None
                        for i in range(7, 0, -1):
                            out_key = f"OUT{i}"
                            if out_key in row and row[out_key] not in ["0:00", "00:00", ""]:
                                last_out = row[out_key]
                                break

                        # Convert last OUT to military time if needed
                        if last_out:
                            match = re.match(r"(\d{1,2}):(\d{2})", last_out)
                            if match:
                                hours, minutes = map(int, match.groups())

                                # Convert to military time if hours are less than 12
                                if hours < 12:
                                    hours += 12

                                last_out = f"{hours:02d}:{minutes:02d}"

                        file_data.append({
                            "access_id": access_id,
                            "DATE": date,
                            "IN": first_in,
                            "OUT": last_out
                        })
                        if first_in or last_out:
                            has_data = 1

        # Append each file's data as a separate entry in extracted_data
        extracted_data.append({
            "file_name": file.original_name,
            "data": file_data,
        })

    # Paginate all extracted data
    page_number = request.GET.get("page", 1)
    paginator = Paginator(extracted_data, 5)  # Show 5 files per page
    page_obj = paginator.get_page(page_number)

    return render(request, "all_data.html", {
        "page_obj": page_obj,
        "instance": instance,
        "has_data": has_data
    })

def download_all(request, instance):
    files = TimeCard.objects.filter(instance=instance)
    rows = []
    emp_order = []

    for file in files:
        if isinstance(file.extracted_data, list):
            for entry in file.extracted_data:
                # Normalize keys and values
                normalized_entry = {
                    key.strip().upper().replace(" ", "").replace("IN", "IN").replace("OUT", "OUT"): 
                    value.strip() if isinstance(value, str) else value
                    for key, value in entry.items()
                }

                access_id = normalized_entry.get("ACCESS_ID", "")
                table_data = normalized_entry.get("TABLE", [])

                if access_id and table_data:
                    if access_id not in emp_order:
                        emp_order.append(access_id)

                    for row in table_data:
                        normalized_row = {
                            key.strip().upper().replace(" ", "").replace("IN", "IN").replace("OUT", "OUT"): 
                            value.strip() if isinstance(value, str) else value
                            for key, value in row.items()
                        }
                        normalized_row["DATE"] = convert_date_format(normalized_row.get("DATE", ""))

                        # First non-empty IN entry that is not "0:00"
                        first_in = next(
                            (
                                str(normalized_row.get(f"IN{i}", "")).strip()
                                for i in range(1, 8)
                                if str(normalized_row.get(f"IN{i}", "")).strip() not in ["", "0:00"]
                            ),
                            ""
                        )

                        # Last non-empty OUT entry that is not "0:00"
                        last_out = next(
                            (
                                str(normalized_row.get(f"OUT{i}", "")).strip()
                                for i in range(7, 0, -1)
                                if str(normalized_row.get(f"OUT{i}", "")).strip() not in ["", "0:00"]
                            ),
                            ""
                        )

                        # Convert last OUT to military time if needed
                        if last_out:
                            match = re.match(r"(\d{1,2}):(\d{2})", last_out)
                            if match:
                                hours, minutes = map(int, match.groups())
                                if hours < 12:
                                    hours += 12  # Assume PM if less than 12
                                last_out = f"{hours:02d}:{minutes:02d}"

                        # Append records
                        if first_in:
                            rows.append([access_id, normalized_row.get("DATE", ""), first_in, 1])
                        if last_out:
                            rows.append([access_id, normalized_row.get("DATE", ""), last_out, 0])

    if not rows:
        return HttpResponse("No attendance data available.", status=400)

    df = pd.DataFrame(rows, columns=["Emp_No", "Attend_Date", "Attend_Time", "Attend_Status"])
    df["Emp_No"] = pd.Categorical(df["Emp_No"], categories=emp_order, ordered=True)
    df.sort_values(by=["Emp_No", "Attend_Status", "Attend_Date"], ascending=[True, False, True], inplace=True)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    cleaned_name = re.sub(r'[^\w\s-]', '', os.path.splitext(file.original_name)[0].strip()).replace(' ', '_')

    response["Content-Disposition"] = f'attachment; filename="Template_for_{cleaned_name}.xlsx"'
    response["X-Content-Type-Options"] = "nosniff"

    with pd.ExcelWriter(response, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False)

    return response




def convert_date_format(date_str):
    # Convert yyyymmdd to mm/dd/yyyy
    try:
        if re.match(r'^\d{8}$', date_str):  # Matches 'yyyymmdd' format
            return datetime.strptime(date_str, "%Y%m%d").strftime("%m/%d/%Y")
    except ValueError:
        pass
    return date_str 

def extract_ever(instance):
    files = TimeCard.objects.filter(instance=instance)

    print(f"Found {files.count()} files for instance: {instance}")

    if files.exists():
        for file in files:
            extracted_data = []  # ✅ Reset extracted_data for each file

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
                        text = page.extract_text()
                        print(f"Page {page_num} text content:\n{text}\n")

                        # Extract Access ID (Emp #) from below the table
                        if text:
                            emp_match = re.search(r"(?:ID:\s*)(\d+)-", text)
                            if emp_match:
                                current_access_id = emp_match.group(1)
                                print(f"Found Emp #: {current_access_id}")

                        extracted_table = page.extract_table()
                        print(f"Page {page_num} extracted table:\n{extracted_table}\n")

                        if extracted_table:
                            if not headers:
                                headers = extracted_table[0]

                            for row in extracted_table[1:]:
                                row_data = {}
                                in_count = 1
                                out_count = 1

                                excess_in = ""
                                excess_out = ""
                                excess_all = []
                                last_out_col = None

                                for i, column_name in enumerate(headers):
                                    value = row[i] if i < len(row) and row[i] else ""

                                    if "DATE" in column_name.upper():
                                        value = value

                                    # Clean value directly with regex
                                    value = re.sub(r'[^0-9:]', '', value.strip())  # Keep only numbers and colons
                                    value = re.sub(r'\s*(am|pm)\s*$', '', value, flags=re.IGNORECASE)  # Remove "am/pm"
                                    

                                    if "IN" in column_name:
                                        if value in ["0:00", "00:00"]:
                                            value = ""  # Filter out invalid times

                                        if len(value) > 5:
                                            row_data[f"IN{in_count}"] = value[:5]
                                            excess_in = value[5:].strip()
                                            excess_all.append(excess_in)
                                        else:
                                            row_data[f"IN{in_count}"] = value
                                            excess_in = None
                                        in_count += 1

                                    elif "OUT" in column_name:
                                        if value in ["0:00", "00:00"]:
                                            value = ""  # Filter out invalid times

                                        if len(value) > 5:
                                            cleaned_value = value[:5]  # Trim to 5 characters
                                            row_data[f"OUT{out_count}"] = cleaned_value if cleaned_value else ""
                                            excess_out = value[5:].strip()
                                            excess_all.append(excess_out)
                                            last_out_col = f"OUT{out_count}"
                                        else:
                                            row_data[f"OUT{out_count}"] = value if value else ""
                                            if value:
                                                last_out_col = f"OUT{out_count}"
                                        out_count += 1

                                    else:
                                        row_data[column_name] = value


                                current_table.append(row_data)

                    if current_access_id and current_table:
                        extracted_data.append({
                            "access_id": current_access_id,
                            "headers": headers,
                            "table": current_table
                        })
                        print(f"Extracted data for Emp #: {current_access_id}: {extracted_data}")

            except Exception as e:
                print(f"Error processing PDF file {file.raw_file.name}: {e}")
                continue

            # ✅ Save only the data for the current file
            if extracted_data:
                for data in extracted_data:
                    for row in data["table"]:
                        if "DATE" in row:
                            row["DATE"] = row["DATE"]

                file.extracted_data = extracted_data
                print(f"Data to be saved: {file.extracted_data}")
                try:
                    file.save()
                    print(f"Successfully saved extracted data for file: {file.raw_file.name}")
                except Exception as e:
                    print(f"Error saving data for file {file.raw_file.name}: {e}")
            else:
                print(f"No valid data found for file: {file.raw_file.name}")

        print(f"Data extraction complete for instance: {instance}")
    else:
        print("No files found for the provided instance.")
        return HttpResponse("No files found for the provided instance.")

def extract_fisher(instance):
    files = TimeCard.objects.filter(instance=instance)

    for file in files:
        records = defaultdict(lambda: {"user_id": None, "times": []})  # ✅ Group by date, store user_id

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
                for page in pdf.pages:
                    text = page.extract_text()
                    if not text:
                        continue

                    for line in text.split("\n"):
                        columns = line.split()

                        if len(columns) < 5:
                            continue

                        try:
                            time_str = f"{columns[1]} {columns[2]} {columns[3]}"
                            time_obj = datetime.strptime(time_str, "%m/%d/%Y %I:%M:%S %p")
                            date_key = time_obj.strftime("%Y%m%d")  # YYYYMMDD format for grouping
                            time_val = time_obj.strftime("%H:%M")  # HH:MM format
                            user_id = columns[4]  # Extract user_id
                        except ValueError:
                            continue

                        records[date_key]["times"].append(time_val)  # ✅ Store timestamps for this date
                        records[date_key]["user_id"] = user_id  # ✅ Ensure access_id is set

        except Exception as e:
            print(f"Error processing PDF file {file.raw_file.name}: {e}")
            continue

        # ✅ Convert grouped records into final format
        extracted_data = []

        for date, data in records.items():
            times = sorted(data["times"])  # ✅ Ensure sorted timestamps
            access_id = data["user_id"]  # ✅ Use extracted user_id as access_id

            # ✅ Convert YYYYMMDD → MM/DD/YYYY
            formatted_date = datetime.strptime(date, "%Y%m%d").strftime("%m/%d/%Y")

            row = {"Date": formatted_date, "Total Hrs": "8"}  # Default total hours

            # ✅ Assign time slots
            for i in range(8):
                key = f"In {i//2 + 1}" if i % 2 == 0 else f"Out {i//2 + 1}"
                row[key] = times[i] if i < len(times) else "0:00"

            row["Status"] = ""  # Add status field
            extracted_data.append(row)

        final_data = [{
            "access_id": access_id,  # ✅ Correctly set access_id
            "headers": ["Date", "Total Hrs", "In 1", "Out 1", "In 2", "Out 2", "In 3", "Out 3", "In 4", "Out 4", "Status"],
            "table": extracted_data
        }]

        # ✅ Save structured data
        if final_data:
            file.extracted_data = final_data
            try:
                file.save()
                print(f"Successfully saved formatted data for {file.raw_file.name}")
            except Exception as e:
                print(f"Error saving data for {file.raw_file.name}: {e}")
        else:
            print(f"No valid data found for {file.raw_file.name}")








