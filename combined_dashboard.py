"""
Combined Timetable Scheduler and Streamlit Dashboard for the IIM Ranchi MBA programme.

This single script includes all functionality required to parse the provided Excel
data, construct course sections, build a conflict graph, schedule sessions and
launch an interactive Streamlit dashboard.  It merges the contents of
``timetable_scheduler.py`` and ``timetable_dashboard.py`` into one file so
that there is no external dependency between modules.  Users can run this
script directly with Streamlit:

    streamlit run combined_dashboard.py

The dashboard allows users to specify parameters such as the maximum class
size, number of weeks, classroom availability before and after the PAN‑IIM
conference, number of sessions per section and the number of timeslots on
each day of the week.  The greedy scheduling algorithm attempts to assign
sessions while respecting student and faculty conflicts and room capacities.

"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple, Iterable, Optional

import pandas as pd
import streamlit as st


###############################################################################
#                            Course and Scheduling Logic                       #
###############################################################################

@dataclass
class CourseSection:
    """Represents a single section of a course.

    Attributes
    ----------
    course_id : str
        Unique identifier composed of the sheet name and a section suffix.
    sheet : str
        Original sheet name from the Excel workbook.
    course_name : str
        Human‑readable course title (with section designation if split).
    faculty_name : str
        Name of the faculty member teaching the section.
    students : List[str]
        List of student names enrolled in this section.
    """

    course_id: str
    sheet: str
    course_name: str
    faculty_name: str
    students: List[str]


def parse_courses(excel_path: str, max_section_size: int = 70) -> List[CourseSection]:
    """Parse the provided Excel workbook into a list of course sections.

    The workbook contains one sheet per course.  Each sheet lists the
    course title, faculty and enrolled students.  If enrolment exceeds
    ``max_section_size`` the course is split into multiple sections.

    Parameters
    ----------
    excel_path : str
        Path to the Excel workbook containing the course data.
    max_section_size : int, optional
        Maximum number of students allowed per section.  Courses with
        enrolment exceeding this number are split into multiple
        sections.  Defaults to 70.

    Returns
    -------
    List[CourseSection]
        A list of section objects ready for scheduling.
    """
    excel = pd.ExcelFile(excel_path)
    sections: List[CourseSection] = []

    for sheet_name in excel.sheet_names:
        df = pd.read_excel(excel_path, sheet_name=sheet_name, header=None)
        course_title: Optional[str] = None
        faculty_name: Optional[str] = None
        header_row_index: Optional[int] = None

        # Identify faculty row and header row
        for i, row in df.iterrows():
            cell = str(row[0]).strip() if pd.notna(row[0]) else ''
            if 'Faculty Name' in cell:
                # Faculty name is in a subsequent cell on the same row
                for j in range(1, len(row)):
                    if pd.notna(row[j]):
                        faculty_name = str(row[j]).strip()
                        break
            if 'Serial No' in cell or cell == 'SN':
                header_row_index = i
                break

        # Extract course title (first non‑empty line that is not a header)
        if course_title is None:
            for i, row in df.iterrows():
                c0 = row[0]
                if pd.notna(c0):
                    text = str(c0).strip()
                    if text not in {'Serial No.', 'Serial No', 'SN', 'SN ', 'Serial No ', 'Group Mail ID'} and 'Faculty Name' not in text:
                        # filter out purely numeric lines
                        if not text.replace('.', '').isdigit():
                            course_title = text
                            break

        # Collect student names starting after the header row
        student_names: List[str] = []
        if header_row_index is not None:
            for i in range(header_row_index + 1, len(df)):
                row = df.loc[i]
                # Consider non‑NA values only
                values = [str(x).strip() for x in row if pd.notna(x)]
                if not values:
                    continue
                # Skip rows with a single value (likely serial number)
                if len(values) == 1:
                    continue
                # Student name is typically in the last or second‑to‑last column
                name = values[-1]
                if '@' in name and len(values) >= 2:
                    # Last column contains email – choose preceding column
                    name = values[-2]
                # If the name resembles an ID (contains '-' and no letters before it), choose previous value
                if '-' in name and not any(c.isalpha() for c in name.split('-')[0]) and len(values) >= 2:
                    name = values[-2]
                student_names.append(name)
        # Remove duplicates while preserving order
        seen = set()
        unique_students: List[str] = []
        for nm in student_names:
            if nm not in seen:
                seen.add(nm)
                unique_students.append(nm)

        # Split into sections as needed
        # Determine how many sections are required based on the maximum allowed size.
        total_students = len(unique_students)
        num_sections = math.ceil(total_students / max_section_size) if max_section_size > 0 else 1
        # If only one section is needed, create it directly
        if num_sections == 1:
            sections.append(
                CourseSection(
                    course_id=f"{sheet_name}_1",
                    sheet=sheet_name,
                    course_name=course_title or sheet_name,
                    faculty_name=faculty_name or '',
                    students=unique_students,
                )
            )
        else:
            # Distribute students as evenly as possible across the sections.  The first
            # `remainder` sections receive one extra student so that the difference
            # between any two section sizes is at most one.
            base_size = total_students // num_sections
            remainder = total_students % num_sections
            start_index = 0
            for idx in range(num_sections):
                size = base_size + (1 if idx < remainder else 0)
                sec_students = unique_students[start_index:start_index + size]
                start_index += size
                # Name sections sequentially: Sec A, Sec B, ...
                section_name = f"Sec {chr(ord('A') + idx)}"
                sections.append(
                    CourseSection(
                        course_id=f"{sheet_name}_Sec{idx+1}",
                        sheet=sheet_name,
                        course_name=f"{course_title or sheet_name} {section_name}",
                        faculty_name=faculty_name or '',
                        students=sec_students,
                    )
                )
    return sections


def build_conflict_graph(sections: Iterable[CourseSection]) -> Dict[str, set]:
    """Construct a conflict graph between course sections.

    Two sections conflict if they share at least one student or have the
    same faculty member.  The returned mapping associates each
    ``course_id`` with the set of conflicting ``course_id`` values.

    Parameters
    ----------
    sections : Iterable[CourseSection]
        The list or iterable of all course sections.

    Returns
    -------
    Dict[str, set]
        Dictionary keyed by course_id with values as sets of conflicting
        course_ids.
    """
    conflict_map: Dict[str, set] = {sec.course_id: set() for sec in sections}
    # Precompute student sets for efficient intersection tests
    student_sets: Dict[str, set] = {sec.course_id: set(sec.students) for sec in sections}
    faculty_map: Dict[str, List[str]] = {}
    for sec in sections:
        faculty_map.setdefault(sec.faculty_name, []).append(sec.course_id)
    # Add conflicts based on shared faculty
    for faculty, sec_ids in faculty_map.items():
        for i in range(len(sec_ids)):
            for j in range(i + 1, len(sec_ids)):
                a, b = sec_ids[i], sec_ids[j]
                conflict_map[a].add(b)
                conflict_map[b].add(a)
    # Add conflicts based on shared students
    sec_list = list(sections)
    for i in range(len(sec_list)):
        for j in range(i + 1, len(sec_list)):
            a, b = sec_list[i].course_id, sec_list[j].course_id
            # Skip if already marked as faculty conflict
            if b in conflict_map[a]:
                continue
            if student_sets[a] & student_sets[b]:
                conflict_map[a].add(b)
                conflict_map[b].add(a)
    return conflict_map


def schedule_sections(
    sections: List[CourseSection],
    conflict_map: Dict[str, set],
    weeks: int = 10,
    rooms_before: int = 10,
    rooms_after: int = 4,
    timeslots_per_day: List[int] | Tuple[int, ...] = (7, 7, 7, 7, 7, 7, 5),
    sessions_per_section: int = 20,
    random_seed: int = 42,
) -> Optional[Tuple[Dict[str, List[Tuple[int, int]]], List[Tuple[int, int]], List[int], List[List[List[str]]]]]:
    """Greedy scheduler for assigning sessions to timeslots.

    For each section, the algorithm attempts to assign ``sessions_per_section``
    sessions across the entire term (not enforcing an even weekly
    distribution).  Sessions are allocated one at a time, preferring
    earlier weeks and under‑utilised timeslots.  A session is placed in
    the first available slot that satisfies both the capacity and
    conflict constraints.  If at any point no slot can be found for a
    session the function returns ``None`` indicating that the problem
    parameters are infeasible (e.g. too many conflicts or too few
    timeslots).

    Parameters
    ----------
    sections : List[CourseSection]
        Course sections to be scheduled.
    conflict_map : Dict[str, set]
        Mapping of each ``course_id`` to the set of conflicting course IDs.
    weeks : int, optional
        Number of teaching weeks in the term.  Default is 10.
    rooms_before : int, optional
        Number of classrooms available before the PAN‑IIM conference (first
        four weeks).  Default is 10.
    rooms_after : int, optional
        Number of classrooms available after the first four weeks.  Default is 4.
    timeslots_per_day : sequence of int, optional
        Number of 1.5‑hour time windows available for each day of the week.
        The sequence should have length 7 (Monday through Sunday).  In
        the default configuration Monday–Saturday have 7 slots and
        Sunday has 5.  Adjust this list to explore alternative
        timetables.
    sessions_per_section : int, optional
        Number of sessions each section must be scheduled for over the
        entire term.  MBA courses typically require 20 sessions.  The
        algorithm distributes these sessions across all weeks without
        requiring an even weekly pattern.
    random_seed : int, optional
        Seed for the pseudo‑random number generator used to break ties.

    Returns
    -------
    Optional[Tuple[Dict[str, List[Tuple[int,int]]], List[Tuple[int,int]], List[int], List[List[List[str]]]]]
        On success returns a tuple containing:

        * ``schedule`` – a mapping from ``course_id`` to a list of ``(week, timeslot_index)`` pairs indicating where each session was placed.
        * ``timeslot_map`` – list mapping each global timeslot index to a ``(day_of_week, slot_of_day)`` tuple.
        * ``week_capacity`` – a list of length ``weeks`` giving the number of rooms available each week.
        * ``assignments_by_timeslot`` – a three‑dimensional structure indexed by ``[week][timeslot_index]`` holding lists of course IDs assigned to that slot.  Useful for assigning rooms after scheduling.

        If the scheduler fails (unable to assign all sessions) the
        function returns ``None``.
    """
    random.seed(random_seed)
    # Build timeslot map (same pattern repeated every week)
    timeslot_map: List[Tuple[int, int]] = []
    for day_idx, count in enumerate(timeslots_per_day):
        for slot_idx in range(count):
            timeslot_map.append((day_idx, slot_idx))
    num_timeslots = len(timeslot_map)

    # Determine room capacities for each week.  The first four weeks
    # use ``rooms_before`` rooms, subsequent weeks use ``rooms_after`` rooms.
    week_capacity: List[int] = [rooms_before] * 4 + [rooms_after] * max(0, weeks - 4)
    # Occupancy matrix: occupancy[w][t] counts how many sessions are in week w, slot t
    occupancy: List[List[int]] = [[0] * num_timeslots for _ in range(weeks)]
    # For each timeslot keep track of which courses are assigned; used for conflict checks
    assignments_by_timeslot: List[List[List[str]]] = [[[] for _ in range(num_timeslots)] for _ in range(weeks)]
    # Output schedule mapping course_id -> list of (week, timeslot)
    schedule: Dict[str, List[Tuple[int, int]]] = {sec.course_id: [] for sec in sections}

    # Sort sections in descending order of number of conflicts and number of students.
    # This heuristic attempts to place "hard" sections first.
    sorted_sections = sorted(
        sections,
        key=lambda sec: (len(conflict_map.get(sec.course_id, [])), len(sec.students)),
        reverse=True,
    )

    for sec in sorted_sections:
        cid = sec.course_id
        for session_index in range(sessions_per_section):
            placed = False
            # Generate list of all (week, timeslot) pairs to consider
            candidates: List[Tuple[int, int]] = [(w, t) for w in range(weeks) for t in range(num_timeslots)]
            # Sort candidates by week (earlier weeks first), then by occupancy (fewer assignments first), then tie break randomly
            candidates.sort(key=lambda x: (x[0], occupancy[x[0]][x[1]], random.random()))
            for week_idx, slot_idx in candidates:
                # Skip if capacity exhausted
                if occupancy[week_idx][slot_idx] >= week_capacity[week_idx]:
                    continue
                # Check conflicts: for all courses already assigned in this slot, ensure none conflict with current section
                conflict = False
                for other_cid in assignments_by_timeslot[week_idx][slot_idx]:
                    if other_cid in conflict_map.get(cid, set()):
                        conflict = True
                        break
                if conflict:
                    continue
                # Assign session
                occupancy[week_idx][slot_idx] += 1
                assignments_by_timeslot[week_idx][slot_idx].append(cid)
                schedule[cid].append((week_idx, slot_idx))
                placed = True
                break
            if not placed:
                # Unable to assign this session; indicate failure
                return None
    return schedule, timeslot_map, week_capacity, assignments_by_timeslot


def assign_rooms(assignments_by_timeslot: List[List[List[str]]], week_capacity: List[int]) -> Dict[Tuple[int, int, str], int]:
    """Assign room numbers to each course session after scheduling.

    Given the assignments by timeslot and the number of rooms available
    each week, assign room numbers 1..R to each section within a
    timeslot.  Rooms are allocated sequentially in the order sections
    were scheduled within that timeslot.  The returned mapping can be
    used to build a detailed timetable.

    Parameters
    ----------
    assignments_by_timeslot : List[List[List[str]]]
        For each week and timeslot, a list of course_ids assigned.
    week_capacity : List[int]
        Number of rooms available in each week.

    Returns
    -------
    Dict[Tuple[int, int, str], int]
        Dictionary mapping (week, timeslot, course_id) to an integer
        room number.  Room numbers start at 1 for each week and timeslot.
    """
    room_assignment: Dict[Tuple[int, int, str], int] = {}
    for week_idx, week_slots in enumerate(assignments_by_timeslot):
        for slot_idx, course_list in enumerate(week_slots):
            # room numbers cycle from 1 up to capacity
            for room_num, cid in enumerate(course_list, start=1):
                room_assignment[(week_idx, slot_idx, cid)] = room_num
    return room_assignment


def build_schedule_dataframe(
    sections: List[CourseSection],
    schedule: Dict[str, List[Tuple[int, int]]],
    timeslot_map: List[Tuple[int, int]],
    week_capacity: List[int],
    assignments_by_timeslot: List[List[List[str]]],
    start_times_weekday: Optional[List[Tuple[str, str]]] = None,
    start_times_sunday: Optional[List[Tuple[str, str]]] = None,
) -> pd.DataFrame:
    """Construct a Pandas DataFrame summarising the schedule.

    Parameters
    ----------
    sections : List[CourseSection]
        All course sections.
    schedule : Dict[str, List[Tuple[int,int]]]
        Mapping from course_id to (week, timeslot) assignments returned by
        ``schedule_sections``.
    timeslot_map : List[Tuple[int,int]]
        Mapping from timeslot index to (day_of_week, slot_of_day).
    week_capacity : List[int]
        Number of rooms available each week (length == number of weeks).
    assignments_by_timeslot : List[List[List[str]]]
        For each week and timeslot, the list of course_ids assigned.
    start_times_weekday : List[Tuple[str,str]], optional
        Human‑readable start and end times for each slot on a weekday.  Must
        have length equal to the maximum number of slots on Monday–Saturday.
    start_times_sunday : List[Tuple[str,str]], optional
        Human‑readable start and end times for each slot on Sunday.  Must have
        length equal to the number of slots on Sunday.

    Returns
    -------
    pd.DataFrame
        DataFrame with columns describing each scheduled session: week,
        day, slot, start/end times, room number, course and faculty.
    """
    # Default time labels if none provided
    if start_times_weekday is None:
        start_times_weekday = [
            ('09:00', '10:30'), ('10:30', '12:00'), ('12:00', '13:30'),
            ('13:30', '15:00'), ('15:00', '16:30'), ('16:30', '18:00'),
            ('18:00', '19:30')
        ]
    if start_times_sunday is None:
        start_times_sunday = [
            ('09:00', '10:30'), ('10:30', '12:00'), ('12:00', '13:30'),
            ('13:30', '15:00'), ('15:00', '16:30')
        ]
    day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    section_lookup: Dict[str, CourseSection] = {sec.course_id: sec for sec in sections}
    # Assign room numbers
    room_assignment = assign_rooms(assignments_by_timeslot, week_capacity)
    records = []
    for cid, assignments in schedule.items():
        sec = section_lookup[cid]
        for (week_idx, slot_idx) in assignments:
            day_idx, slot_of_day = timeslot_map[slot_idx]
            # Determine start and end times
            if day_idx == 6:  # Sunday
                if slot_of_day < len(start_times_sunday):
                    start_time, end_time = start_times_sunday[slot_of_day]
                else:
                    start_time, end_time = ('', '')
            else:
                if slot_of_day < len(start_times_weekday):
                    start_time, end_time = start_times_weekday[slot_of_day]
                else:
                    start_time, end_time = ('', '')
            room_num = room_assignment.get((week_idx, slot_idx, cid), 1)
            records.append({
                'Week': week_idx + 1,
                'Week_Name': f'Week {week_idx + 1}',
                'Day': day_idx,
                'Day_Name': day_names[day_idx],
                'Slot': slot_of_day + 1,
                'Start_Time': start_time,
                'End_Time': end_time,
                'Room': room_num,
                'Course_ID': cid,
                'Course_Name': sec.course_name,
                'Faculty': sec.faculty_name,
                'Section': cid.split('_')[-1] if 'Sec' in cid else '1',
                'Students': len(sec.students),
            })
    # Create DataFrame
    df = pd.DataFrame.from_records(records)
    # Sort for readability: by Week, Day, Start_Time, Room
    df.sort_values(['Week', 'Day', 'Slot', 'Room'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


###############################################################################
#                             Streamlit Dashboard                              #
###############################################################################

@st.cache_data(show_spinner=False)
def load_course_sections(path: str, max_section_size: int) -> tuple[list, dict]:
    """Load course sections and the associated conflict graph.

    This function is cached by Streamlit so that repeated runs do not
    re‑read the Excel file on every interaction.  If the maximum
    section size is changed the cache is invalidated.
    """
    sections = parse_courses(path, max_section_size=max_section_size)
    conflict_map = build_conflict_graph(sections)
    return sections, conflict_map


def main() -> None:
    """Entry point for launching the Streamlit dashboard."""
    st.set_page_config(page_title="MBA Timetable Scheduler", layout="wide")
    st.title("IIM Ranchi MBA Timetable Scheduler")
    st.markdown(
        "This dashboard generates a conflict‑free teaching timetable for the MBA programme at IIM Ranchi. "
        "Adjust the parameters on the sidebar and click *Generate Schedule* to see the resulting timetable."
    )
    # Sidebar for configuration
    st.sidebar.header("Configuration")
    data_file = st.sidebar.text_input("Path to Excel data", value="WAI_Data.xlsx")
    max_section_size = st.sidebar.number_input("Maximum section size", min_value=50, max_value=150, value=70, step=5)
    weeks = st.sidebar.number_input("Number of weeks", min_value=1, max_value=20, value=10, step=1)
    rooms_before = st.sidebar.number_input("Rooms before conference", min_value=1, max_value=20, value=10, step=1)
    rooms_after = st.sidebar.number_input("Rooms after conference", min_value=1, max_value=20, value=4, step=1)
    sessions_per_section = st.sidebar.number_input(
        "Sessions per course section", min_value=5, max_value=30, value=20, step=1
    )
    random_seed = st.sidebar.number_input("Random seed", min_value=0, max_value=9999, value=42, step=1)
    st.sidebar.markdown("### Timeslots per day")
    default_slots = [7, 7, 7, 7, 7, 7, 5]
    day_labels = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    timeslots_per_day: List[int] = []
    for i, day_name in enumerate(day_labels):
        slots = st.sidebar.number_input(
            f"{day_name} slots", min_value=1, max_value=10, value=default_slots[i], step=1, key=f"slots_{day_name}"
        )
        timeslots_per_day.append(slots)

    # Load data
    excel_path = data_file
    # Check existence of the Excel file
    if not pd.io.common.file_exists(excel_path):
        st.error(f"Cannot find the Excel data file at '{data_file}'.")
        return
    with st.spinner("Loading course data..."):
        sections, conflict_map = load_course_sections(str(excel_path), int(max_section_size))
    st.sidebar.markdown(f"**Total courses:** {len(sections)}")
    st.sidebar.markdown(
        f"**Total required sessions:** {len(sections) * int(sessions_per_section)}"
    )
    # Generate schedule on button press
    if st.sidebar.button("Generate Schedule"):
        with st.spinner("Scheduling sessions..."):
            result = schedule_sections(
                sections,
                conflict_map,
                weeks=int(weeks),
                rooms_before=int(rooms_before),
                rooms_after=int(rooms_after),
                timeslots_per_day=timeslots_per_day,
                sessions_per_section=int(sessions_per_section),
                random_seed=int(random_seed),
            )
        if result is None:
            st.error(
                "Unable to build a feasible schedule with the given parameters. "
                "Try increasing the number of timeslots, reducing sessions per course or adjusting room counts."
            )
        else:
            # Unpack scheduling result
            schedule, timeslot_map, week_capacity, assignments_by_timeslot = result
            # Convert to DataFrame for display
            df = build_schedule_dataframe(
                sections,
                schedule,
                timeslot_map,
                week_capacity,
                assignments_by_timeslot,
            )
            st.success("Schedule generated successfully!")
            # Pre‑compute summary metrics for later use
            # Sessions per week (1-indexed weeks)
            sessions_per_week = df.groupby('Week').size()
            # Build a DataFrame showing the maximum available rooms and actual sessions for each week
            week_range = list(range(1, len(week_capacity) + 1))
            capacity_df = pd.DataFrame({
                'Week': week_range,
                'Capacity': week_capacity,
                'Sessions': [sessions_per_week.get(w, 0) for w in week_range],
            })
            # Faculty workload: number of sessions per faculty
            faculty_counts = df['Faculty'].value_counts().reset_index()
            faculty_counts.columns = ['Faculty', 'Sessions']
            # Class distribution by day of week
            day_labels_full = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            day_counts = df['Day_Name'].value_counts().reindex(day_labels_full, fill_value=0)
            day_df = pd.DataFrame({'Day': day_labels_full, 'Sessions': day_counts.values})

            # Create separate tabs for timetable and insights
            tab_timetable, tab_insights = st.tabs(["Timetable", "Insights"])

            # --- Timetable Tab ---
            with tab_timetable:
                st.subheader("Timetable")
                # Filters for week, day, course and faculty
                # Determine unique options for multiselects
                week_options = sorted(df['Week'].unique().tolist())
                day_options = day_labels_full  # Keep full week order even if some days are absent
                course_options = sorted(df['Course_Name'].unique().tolist())
                faculty_options = sorted(df['Faculty'].unique().tolist())
                selected_weeks = st.multiselect("Filter by week", options=week_options, default=week_options)
                selected_days = st.multiselect("Filter by day", options=day_options, default=day_options)
                selected_courses = st.multiselect("Filter by course", options=course_options, default=course_options)
                selected_faculties = st.multiselect("Filter by faculty", options=faculty_options, default=faculty_options)
                # Apply filters
                filtered_df = df[
                    df['Week'].isin(selected_weeks) &
                    df['Day_Name'].isin(selected_days) &
                    df['Course_Name'].isin(selected_courses) &
                    df['Faculty'].isin(selected_faculties)
                ]
                # Show filtered timetable
                st.dataframe(
                    filtered_df,
                    use_container_width=True,
                    column_config={
                        'Week': 'Week',
                        'Day_Name': 'Day',
                        'Start_Time': 'Start',
                        'End_Time': 'End',
                        'Room': 'Room',
                        'Course_Name': 'Course',
                        'Faculty': 'Faculty',
                        'Section': 'Sec',
                    },
                    hide_index=True,
                )
                # Download buttons
                import io
                csv_full = df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="Download full schedule (CSV)",
                    data=csv_full,
                    file_name="schedule_full.csv",
                    mime="text/csv",
                )
                csv_filtered = filtered_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="Download filtered schedule (CSV)",
                    data=csv_filtered,
                    file_name="schedule_filtered.csv",
                    mime="text/csv",
                )

            # --- Insights Tab ---
            with tab_insights:
                st.subheader("Insights")
                # Weekly capacity vs sessions chart
                st.markdown("**Weekly Room Capacity vs Sessions Scheduled**")
                # Use Altair for dual line chart if available else separate charts
                try:
                    import altair as alt  # type: ignore
                    # Melt the capacity_df for a single chart
                    chart_df = capacity_df.melt(id_vars=['Week'], value_vars=['Capacity', 'Sessions'], var_name='Metric', value_name='Value')
                    capacity_chart = alt.Chart(chart_df).mark_line(point=True).encode(
                        x=alt.X('Week:O', title='Week'),
                        y=alt.Y('Value:Q', title='Number of Sessions / Rooms'),
                        color='Metric:N',
                        tooltip=['Week', 'Metric', 'Value']
                    ).properties(width=600, height=300)
                    st.altair_chart(capacity_chart, use_container_width=True)
                except Exception:
                    # Fallback to simple bar and line charts
                    st.bar_chart(capacity_df.set_index('Week')[['Sessions', 'Capacity']])

                # Faculty workload chart
                st.markdown("**Faculty Workload (Sessions per Faculty)**")
                # Show top faculties or all if less than 20
                st.bar_chart(faculty_counts.set_index('Faculty')['Sessions'])

                # Distribution by day of week
                st.markdown("**Class Distribution by Day of Week**")
                st.bar_chart(day_df.set_index('Day')['Sessions'])

    else:
        st.info("Adjust parameters on the left and click *Generate Schedule* to create a timetable.")


if __name__ == "__main__":
    main()