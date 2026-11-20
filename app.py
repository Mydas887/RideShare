import json
import math
import sqlite3
from datetime import datetime, date, time, timedelta

import folium
import pandas as pd
import requests
import streamlit as st
from geopy.distance import geodesic
from streamlit_folium import st_folium


# ============================================================
# CONFIGURATION
# ============================================================

DB_NAME = "rideshare.db"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

USER_AGENT = "RideShareMVP/1.0"

MAX_ROUTE_DISTANCE_KM = 5.0
EXCELLENT_ROUTE_DISTANCE_KM = 1.0
GOOD_ROUTE_DISTANCE_KM = 3.0

MAX_TIME_DIFFERENCE_MINUTES = 60

ROUTE_WEIGHT = 0.45
TIME_WEIGHT = 0.25
DESTINATION_WEIGHT = 0.20
SEAT_WEIGHT = 0.10


# ============================================================
# STREAMLIT CONFIG / CSS
# ============================================================

st.set_page_config(
    page_title="RideShare MVP",
    page_icon="🚗",
    layout="wide",
)

st.markdown(
    """
    <style>
    .ride-card {
        padding: 1rem;
        border: 1px solid #ddd;
        border-radius: 12px;
        margin-bottom: 1rem;
        background: #ffffff;
    }
    .match-score {
        font-size: 1.8rem;
        font-weight: 700;
    }
    .muted {
        color: #666;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# DATABASE
# ============================================================

def get_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            organization_type TEXT NOT NULL,
            email_domain TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            organization_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            is_verified INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (organization_id) REFERENCES organizations(id)
        );

        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            make TEXT,
            model TEXT,
            color TEXT,
            license_plate TEXT,
            seats INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS rides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            organization_id INTEGER NOT NULL,
            origin_address TEXT NOT NULL,
            origin_lat REAL NOT NULL,
            origin_lon REAL NOT NULL,
            destination_address TEXT NOT NULL,
            destination_lat REAL NOT NULL,
            destination_lon REAL NOT NULL,
            departure_date TEXT NOT NULL,
            departure_time TEXT NOT NULL,
            available_seats INTEGER NOT NULL,
            total_seats INTEGER NOT NULL,
            route_distance_km REAL,
            route_duration_min REAL,
            route_geometry TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            FOREIGN KEY (driver_id) REFERENCES users(id),
            FOREIGN KEY (organization_id) REFERENCES organizations(id)
        );

        CREATE TABLE IF NOT EXISTS ride_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL,
            passenger_id INTEGER NOT NULL,
            pickup_address TEXT NOT NULL,
            pickup_lat REAL NOT NULL,
            pickup_lon REAL NOT NULL,
            requested_date TEXT NOT NULL,
            requested_time TEXT NOT NULL,
            match_score REAL,
            match_reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY (ride_id) REFERENCES rides(id),
            FOREIGN KEY (passenger_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS ride_passengers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL,
            passenger_id INTEGER NOT NULL,
            pickup_address TEXT NOT NULL,
            pickup_lat REAL NOT NULL,
            pickup_lon REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            UNIQUE(ride_id, passenger_id),
            FOREIGN KEY (ride_id) REFERENCES rides(id),
            FOREIGN KEY (passenger_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (ride_id) REFERENCES rides(id),
            FOREIGN KEY (sender_id) REFERENCES users(id),
            FOREIGN KEY (receiver_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL,
            reviewer_id INTEGER NOT NULL,
            reviewed_user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(ride_id, reviewer_id, reviewed_user_id),
            FOREIGN KEY (ride_id) REFERENCES rides(id)
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL,
            reported_user_id INTEGER NOT NULL,
            ride_id INTEGER,
            reason TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'Open',
            created_at TEXT NOT NULL,
            FOREIGN KEY (reporter_id) REFERENCES users(id),
            FOREIGN KEY (reported_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS blocked_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blocker_id INTEGER NOT NULL,
            blocked_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(blocker_id, blocked_user_id),
            FOREIGN KEY (blocker_id) REFERENCES users(id),
            FOREIGN KEY (blocked_user_id) REFERENCES users(id)
        );
        """
    )

    conn.commit()
    conn.close()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def execute(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(query, params)

    result = None
    if fetchone:
        result = cur.fetchone()
    elif fetchall:
        result = cur.fetchall()

    if commit:
        conn.commit()

    conn.close()
    return result


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_organizations():
    return execute(
        "SELECT * FROM organizations ORDER BY name",
        fetchall=True,
    )


def get_user(user_id):
    return execute(
        """
        SELECT u.*, o.name AS organization_name
        FROM users u
        JOIN organizations o ON o.id = u.organization_id
        WHERE u.id = ?
        """,
        (user_id,),
        fetchone=True,
    )


def get_user_by_email(email):
    return execute(
        "SELECT * FROM users WHERE LOWER(email) = LOWER(?)",
        (email.strip(),),
        fetchone=True,
    )


def get_vehicle(user_id):
    return execute(
        "SELECT * FROM vehicles WHERE user_id = ?",
        (user_id,),
        fetchone=True,
    )


def create_notification(user_id, title, message):
    execute(
        """
        INSERT INTO notifications (user_id, title, message, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, title, message, now_iso()),
        commit=True,
    )


def get_unread_notifications(user_id):
    return execute(
        """
        SELECT * FROM notifications
        WHERE user_id = ? AND is_read = 0
        ORDER BY created_at DESC
        """,
        (user_id,),
        fetchall=True,
    )


# ============================================================
# GEOCODING / ROUTING
# ============================================================

def geocode_address(address):
    """Convert a human-readable address into latitude/longitude."""
    if not address or not address.strip():
        return None

    try:
        response = requests.get(
            NOMINATIM_URL,
            params={
                "q": address.strip(),
                "format": "json",
                "limit": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        response.raise_for_status()

        results = response.json()
        if not results:
            return None

        return {
            "lat": float(results[0]["lat"]),
            "lon": float(results[0]["lon"]),
            "display_name": results[0].get("display_name", address),
        }
    except requests.RequestException:
        return None
    except (ValueError, KeyError, TypeError):
        return None


def get_route(origin_lat, origin_lon, destination_lat, destination_lon):
    """Get a driving route from OSRM."""
    url = (
        f"{OSRM_URL}/"
        f"{origin_lon},{origin_lat};{destination_lon},{destination_lat}"
    )

    try:
        response = requests.get(
            url,
            params={
                "overview": "full",
                "geometries": "geojson",
            },
            timeout=20,
        )
        response.raise_for_status()

        data = response.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            return None

        route = data["routes"][0]

        return {
            "distance_km": route["distance"] / 1000,
            "duration_min": route["duration"] / 60,
            "geometry": route["geometry"],
        }

    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def geometry_to_latlon(geometry):
    """Convert GeoJSON coordinates [lon, lat] into [(lat, lon), ...]."""
    if not geometry:
        return []

    return [
        (float(coord[1]), float(coord[0]))
        for coord in geometry.get("coordinates", [])
    ]


def calculate_point_to_route_distance(pickup_lat, pickup_lon, geometry):
    """
    MVP route proximity calculation.

    It compares the passenger pickup point with the route's sampled
    coordinates and returns the minimum distance in kilometers.
    """
    route_points = geometry_to_latlon(geometry)

    if not route_points:
        return None

    pickup = (pickup_lat, pickup_lon)

    return min(
        geodesic(pickup, point).km
        for point in route_points
    )


# ============================================================
# MATCHING ENGINE
# ============================================================

def calculate_route_score(distance_km):
    if distance_km is None or distance_km > MAX_ROUTE_DISTANCE_KM:
        return 0

    if distance_km <= EXCELLENT_ROUTE_DISTANCE_KM:
        return 100

    if distance_km <= GOOD_ROUTE_DISTANCE_KM:
        return 100 - (
            (distance_km - EXCELLENT_ROUTE_DISTANCE_KM)
            / (GOOD_ROUTE_DISTANCE_KM - EXCELLENT_ROUTE_DISTANCE_KM)
        ) * 25

    return 75 - (
        (distance_km - GOOD_ROUTE_DISTANCE_KM)
        / (MAX_ROUTE_DISTANCE_KM - GOOD_ROUTE_DISTANCE_KM)
    ) * 75


def calculate_time_score(driver_time, passenger_time):
    difference = abs(
        (
            datetime.combine(date.today(), driver_time)
            - datetime.combine(date.today(), passenger_time)
        ).total_seconds()
    ) / 60

    if difference > MAX_TIME_DIFFERENCE_MINUTES:
        return 0, difference

    if difference <= 10:
        return 100, difference

    if difference <= 20:
        return 90, difference

    if difference <= 30:
        return 75, difference

    return max(
        0,
        75 * (
            1
            - (difference - 30)
            / (MAX_TIME_DIFFERENCE_MINUTES - 30)
        ),
    ), difference


def calculate_seat_score(available_seats, requested_seats):
    if available_seats < requested_seats:
        return 0

    if available_seats >= 3:
        return 100

    if available_seats == 2:
        return 90

    return 80


def calculate_match_score(
    route_distance_km,
    driver_time,
    passenger_time,
    available_seats,
    requested_seats,
):
    route_score = calculate_route_score(route_distance_km)

    time_score, time_difference = calculate_time_score(
        driver_time,
        passenger_time,
    )

    seat_score = calculate_seat_score(
        available_seats,
        requested_seats,
    )

    destination_score = 100

    final_score = (
        route_score * ROUTE_WEIGHT
        + time_score * TIME_WEIGHT
        + destination_score * DESTINATION_WEIGHT
        + seat_score * SEAT_WEIGHT
    )

    return {
        "score": round(final_score),
        "route_score": round(route_score),
        "time_score": round(time_score),
        "destination_score": destination_score,
        "seat_score": round(seat_score),
        "time_difference": round(time_difference),
    }


def generate_match_reason(match, distance_km):
    reasons = []

    if distance_km <= 1:
        reasons.append(
            f"Pickup is only {distance_km:.1f} km from the driver's route."
        )
    elif distance_km <= 3:
        reasons.append(
            f"Pickup is {distance_km:.1f} km from the driver's route."
        )
    else:
        reasons.append(
            f"Pickup is {distance_km:.1f} km from the driver's route."
        )

    if match["time_difference"] <= 10:
        reasons.append(
            f"Departure time is {match['time_difference']} minutes "
            "from your requested time."
        )
    elif match["time_difference"] <= 30:
        reasons.append(
            f"Departure time differs by {match['time_difference']} minutes."
        )

    reasons.append("Same organization.")
    reasons.append(
        f"{match['seat_score']}% seat availability score."
    )

    return reasons


def find_matches(
    organization_id,
    pickup_address,
    requested_date,
    requested_time,
    requested_seats,
):
    pickup = geocode_address(pickup_address)

    if not pickup:
        return None, "We could not find that pickup location."

    rides = execute(
        """
        SELECT
            r.*,
            u.name AS driver_name,
            u.id AS driver_user_id
        FROM rides r
        JOIN users u ON u.id = r.driver_id
        WHERE r.organization_id = ?
          AND r.status = 'active'
          AND r.departure_date = ?
          AND r.available_seats >= ?
        ORDER BY r.departure_time
        """,
        (
            organization_id,
            requested_date.isoformat(),
            requested_seats,
        ),
        fetchall=True,
    )

    results = []

    for ride in rides:
        # Skip users blocked by the current passenger.
        blocked = execute(
            """
            SELECT id FROM blocked_users
            WHERE blocker_id = ? AND blocked_user_id = ?
            """,
            (st.session_state["user_id"], ride["driver_id"]),
            fetchone=True,
        )

        if blocked:
            continue

        try:
            driver_time = datetime.strptime(
                ride["departure_time"],
                "%H:%M",
            ).time()

            passenger_time = requested_time
        except ValueError:
            continue

        try:
            geometry = json.loads(ride["route_geometry"])
        except (TypeError, json.JSONDecodeError):
            continue

        distance_km = calculate_point_to_route_distance(
            pickup["lat"],
            pickup["lon"],
            geometry,
        )

        if distance_km is None or distance_km > MAX_ROUTE_DISTANCE_KM:
            continue

        match = calculate_match_score(
            distance_km,
            driver_time,
            passenger_time,
            ride["available_seats"],
            requested_seats,
        )

        if match["time_score"] <= 0:
            continue

        reasons = generate_match_reason(match, distance_km)

        results.append(
            {
                "ride": ride,
                "pickup": pickup,
                "distance_km": distance_km,
                "match": match,
                "reasons": reasons,
            }
        )

    results.sort(
        key=lambda x: x["match"]["score"],
        reverse=True,
    )

    return results, None


# ============================================================
# MAPS
# ============================================================

def create_route_map(
    origin,
    destination,
    route_geometry,
    pickup=None,
):
    points = geometry_to_latlon(route_geometry)

    if not points:
        return None

    center = points[len(points) // 2]

    fmap = folium.Map(
        location=center,
        zoom_start=12,
        control_scale=True,
    )

    folium.Marker(
        [origin["lat"], origin["lon"]],
        tooltip="Origin",
        icon=folium.Icon(color="green", icon="play"),
    ).add_to(fmap)

    folium.Marker(
        [destination["lat"], destination["lon"]],
        tooltip="Destination",
        icon=folium.Icon(color="red", icon="flag"),
    ).add_to(fmap)

    folium.PolyLine(
        points,
        weight=5,
        opacity=0.8,
        tooltip="Driver Route",
    ).add_to(fmap)

    if pickup:
        folium.Marker(
            [pickup["lat"], pickup["lon"]],
            tooltip="Passenger Pickup",
            icon=folium.Icon(color="blue", icon="user"),
        ).add_to(fmap)

    fmap.fit_bounds(points)

    return fmap


# ============================================================
# AUTHENTICATION
# ============================================================

def login_user(email, password):
    user = get_user_by_email(email)

    if not user:
        return None, "Account not found."

    if user["password"] != password:
        return None, "Incorrect password."

    return user, None


def set_logged_in_user(user):
    st.session_state["user_id"] = user["id"]
    st.session_state["user_name"] = user["name"]
    st.session_state["organization_id"] = user["organization_id"]
    st.session_state["role"] = user["role"]


def logout():
    for key in [
        "user_id",
        "user_name",
        "organization_id",
        "role",
        "selected_ride",
        "search_results",
    ]:
        st.session_state.pop(key, None)


def is_logged_in():
    return "user_id" in st.session_state


# ============================================================
# DEMO DATA
# ============================================================

def seed_demo_data():
    existing = execute(
        "SELECT COUNT(*) AS count FROM organizations",
        fetchone=True,
    )

    if existing["count"] > 0:
        return False, "Demo data was not loaded because data already exists."

    organizations = [
        ("NUST", "University", "nust.edu.pk"),
        ("FAST University", "University", "nu.edu.pk"),
        ("COMSATS", "University", "comsats.edu.pk"),
        ("ABC Company", "Workplace", "abc.com"),
    ]

    for name, org_type, domain in organizations:
        execute(
            """
            INSERT INTO organizations
            (name, organization_type, email_domain, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (name, org_type, domain, now_iso()),
            commit=True,
        )

    nust = execute(
        "SELECT id FROM organizations WHERE name = 'NUST'",
        fetchone=True,
    )["id"]

    users = [
        ("Ahmed", "ahmed@demo.com", "1234", nust, "both"),
        ("Sara", "sara@demo.com", "1234", nust, "passenger"),
        ("Usman", "usman@demo.com", "1234", nust, "driver"),
        ("Admin", "admin@demo.com", "admin123", nust, "admin"),
    ]

    user_ids = {}

    for name, email, password, org_id, role in users:
        execute(
            """
            INSERT INTO users
            (name, email, password, organization_id, role, is_verified, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (name, email, password, org_id, role, now_iso()),
            commit=True,
        )

        user = get_user_by_email(email)
        user_ids[email] = user["id"]

    # Vehicle for Ahmed.
    execute(
        """
        INSERT INTO vehicles
        (user_id, make, model, color, license_plate, seats, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_ids["ahmed@demo.com"],
            "Toyota",
            "Corolla",
            "White",
            "DEMO-123",
            4,
            now_iso(),
        ),
        commit=True,
    )

    # Real coordinates around Islamabad for a working demo.
    origin = geocode_address("F-10 Islamabad")
    destination = geocode_address("NUST H-12 Islamabad")

    if not origin or not destination:
        return False, "Organizations/users were created, but demo route geocoding failed."

    route = get_route(
        origin["lat"],
        origin["lon"],
        destination["lat"],
        destination["lon"],
    )

    if not route:
        return False, "Demo accounts were created, but OSRM route calculation failed."

    tomorrow = date.today() + timedelta(days=1)

    execute(
        """
        INSERT INTO rides (
            driver_id,
            organization_id,
            origin_address,
            origin_lat,
            origin_lon,
            destination_address,
            destination_lat,
            destination_lon,
            departure_date,
            departure_time,
            available_seats,
            total_seats,
            route_distance_km,
            route_duration_min,
            route_geometry,
            notes,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_ids["ahmed@demo.com"],
            nust,
            "F-10 Islamabad",
            origin["lat"],
            origin["lon"],
            "NUST H-12 Islamabad",
            destination["lat"],
            destination["lon"],
            tomorrow.isoformat(),
            "08:00",
            3,
            3,
            route["distance_km"],
            route["duration_min"],
            json.dumps(route["geometry"]),
            "Demo ride for testing route matching.",
            "active",
            now_iso(),
        ),
        commit=True,
    )

    return True, (
        "Demo data loaded successfully. "
        "Login with ahmed@demo.com / 1234 or "
        "sara@demo.com / 1234."
    )


# ============================================================
# UI HELPERS
# ============================================================

def require_login():
    if not is_logged_in():
        st.warning("Please log in first.")
        return False
    return True


def get_user_rating(user_id):
    row = execute(
        """
        SELECT AVG(rating) AS average_rating, COUNT(*) AS rating_count
        FROM ratings
        WHERE reviewed_user_id = ?
        """,
        (user_id,),
        fetchone=True,
    )

    if not row or row["average_rating"] is None:
        return None

    return round(row["average_rating"], 1)


def get_active_rides_for_user(user_id):
    return execute(
        """
        SELECT * FROM rides
        WHERE driver_id = ?
        ORDER BY departure_date, departure_time
        """,
        (user_id,),
        fetchall=True,
    )


# ============================================================
# PAGES
# ============================================================

def page_auth():
    st.title("🚗 RideShare")
    st.subheader("Login or create an account")

    login_tab, register_tab = st.tabs(["Login", "Register"])

    with login_tab:
        with st.form("login_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)

        if submitted:
            user, error = login_user(email, password)

            if error:
                st.error(error)
            else:
                set_logged_in_user(user)
                st.success(f"Welcome, {user['name']}!")
                st.rerun()

    with register_tab:
        organizations = get_organizations()

        if not organizations:
            st.info("No organizations exist yet. Please create one below.")
            create_org_form()

        organizations = get_organizations()

        with st.form("register_form"):
            name = st.text_input("Full Name")
            email = st.text_input("Email", key="register_email")
            password = st.text_input("Password", type="password", key="register_password")

            org_options = {
                org["name"]: org["id"]
                for org in organizations
            }

            selected_org = st.selectbox(
                "Organization",
                list(org_options.keys()),
            )

            role = st.selectbox(
                "Role",
                ["passenger", "driver", "both"],
            )

            verified = st.checkbox(
                "MVP Verification",
                value=True,
                help="This is simulated verification for the prototype.",
            )

            submitted = st.form_submit_button(
                "Create Account",
                use_container_width=True,
            )

        if submitted:
            if not name.strip() or not email.strip() or not password:
                st.error("Please complete all required fields.")
            elif get_user_by_email(email):
                st.error("An account with this email already exists.")
            else:
                execute(
                    """
                    INSERT INTO users
                    (name, email, password, organization_id, role, is_verified, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name.strip(),
                        email.strip(),
                        password,
                        org_options[selected_org],
                        role,
                        1 if verified else 0,
                        now_iso(),
                    ),
                    commit=True,
                )
                st.success("Account created. Please log in.")


def create_org_form():
    with st.form("create_org"):
        name = st.text_input("Organization Name")
        org_type = st.selectbox(
            "Organization Type",
            ["University", "College", "Workplace", "Other"],
        )
        domain = st.text_input("Email Domain (optional)")
        submitted = st.form_submit_button("Create Organization")

    if submitted:
        if not name.strip():
            st.error("Organization name is required.")
            return

        try:
            execute(
                """
                INSERT INTO organizations
                (name, organization_type, email_domain, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name.strip(), org_type, domain.strip(), now_iso()),
                commit=True,
            )
            st.success("Organization created.")
            st.rerun()
        except sqlite3.IntegrityError:
            st.error("That organization already exists.")


def page_home():
    user = get_user(st.session_state["user_id"])

    st.title("🚗 RideShare")
    st.subheader(
        "Find people from your organization who are going your way."
    )

    c1, c2, c3, c4 = st.columns(4)

    active_rides = execute(
        """
        SELECT COUNT(*) AS count
        FROM rides
        WHERE driver_id = ? AND status = 'active'
        """,
        (user["id"],),
        fetchone=True,
    )["count"]

    pending = execute(
        """
        SELECT COUNT(*) AS count
        FROM ride_requests rr
        JOIN rides r ON r.id = rr.ride_id
        WHERE (r.driver_id = ? OR rr.passenger_id = ?)
          AND rr.status = 'pending'
        """,
        (user["id"], user["id"]),
        fetchone=True,
    )["count"]

    accepted = execute(
        """
        SELECT COUNT(*) AS count
        FROM ride_passengers
        WHERE passenger_id = ? AND status = 'active'
        """,
        (user["id"],),
        fetchone=True,
    )["count"]

    unread = len(get_unread_notifications(user["id"]))

    c1.metric("Active Rides", active_rides)
    c2.metric("Pending Requests", pending)
    c3.metric("Joined Rides", accepted)
    c4.metric("Notifications", unread)

    st.divider()

    st.write(f"### Welcome, {user['name']}")
    st.write(f"**Organization:** {user['organization_name']}")
    st.write(
        f"**Verification:** {'✓ Verified' if user['is_verified'] else 'Pending'}"
    )

    col1, col2 = st.columns(2)

    with col1:
        if st.button("🚗 Offer a Ride", use_container_width=True):
            st.session_state["page"] = "Offer a Ride"
            st.rerun()

    with col2:
        if st.button("🔎 Find a Ride", use_container_width=True):
            st.session_state["page"] = "Find a Ride"
            st.rerun()

    st.divider()

    if st.button("🧪 Load Demo Data", use_container_width=True):
        ok, message = seed_demo_data()
        if ok:
            st.success(message)
        else:
            st.warning(message)


def page_profile():
    user = get_user(st.session_state["user_id"])
    vehicle = get_vehicle(user["id"])

    st.title("👤 Profile")

    with st.form("profile_form"):
        name = st.text_input("Name", value=user["name"])
        email = st.text_input("Email", value=user["email"])
        role = st.selectbox(
            "Role",
            ["passenger", "driver", "both"],
            index=["passenger", "driver", "both"].index(user["role"])
            if user["role"] in ["passenger", "driver", "both"]
            else 0,
        )

        submitted = st.form_submit_button("Save Profile")

    if submitted:
        try:
            execute(
                """
                UPDATE users
                SET name = ?, email = ?, role = ?
                WHERE id = ?
                """,
                (name.strip(), email.strip(), role, user["id"]),
                commit=True,
            )
            st.session_state["user_name"] = name.strip()
            st.success("Profile updated.")
            st.rerun()
        except sqlite3.IntegrityError:
            st.error("That email is already in use.")

    st.divider()

    st.subheader("Vehicle")

    vehicle_defaults = vehicle if vehicle else {}

    with st.form("vehicle_form"):
        make = st.text_input(
            "Make",
            value=vehicle_defaults.get("make", ""),
        )
        model = st.text_input(
            "Model",
            value=vehicle_defaults.get("model", ""),
        )
        color = st.text_input(
            "Color",
            value=vehicle_defaults.get("color", ""),
        )
        plate = st.text_input(
            "License Plate",
            value=vehicle_defaults.get("license_plate", ""),
        )
        seats = st.number_input(
            "Number of Seats",
            min_value=1,
            max_value=20,
            value=int(vehicle_defaults.get("seats", 4)),
        )

        submitted = st.form_submit_button("Save Vehicle")

    if submitted:
        if vehicle:
            execute(
                """
                UPDATE vehicles
                SET make = ?, model = ?, color = ?, license_plate = ?, seats = ?
                WHERE user_id = ?
                """,
                (make, model, color, plate, seats, user["id"]),
                commit=True,
            )
        else:
            execute(
                """
                INSERT INTO vehicles
                (user_id, make, model, color, license_plate, seats, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], make, model, color, plate, seats, now_iso()),
                commit=True,
            )

        st.success("Vehicle saved.")


def page_offer_ride():
    user = get_user(st.session_state["user_id"])

    st.title("🚗 Offer a Ride")

    with st.form("offer_ride_form"):
        origin_address = st.text_input(
            "Starting Location",
            placeholder="F-10 Islamabad",
        )

        destination_address = st.text_input(
            "Destination",
            placeholder="NUST H-12 Islamabad",
        )

        departure_date = st.date_input(
            "Date",
            min_value=date.today(),
            value=date.today() + timedelta(days=1),
        )

        departure_time = st.time_input(
            "Departure Time",
            value=time(8, 0),
        )

        available_seats = st.number_input(
            "Available Seats",
            min_value=1,
            max_value=20,
            value=3,
        )

        notes = st.text_area("Notes (optional)")

        calculate = st.form_submit_button(
            "Calculate Route",
            use_container_width=True,
        )

    if calculate:
        if not origin_address.strip() or not destination_address.strip():
            st.error("Please enter both locations.")
            return

        with st.spinner("Finding locations..."):
            origin = geocode_address(origin_address)
            destination = geocode_address(destination_address)

        if not origin:
            st.error(
                "We could not find the starting location. "
                "Try a more specific address."
            )
            return

        if not destination:
            st.error(
                "We could not find the destination. "
                "Try a more specific address."
            )
            return

        with st.spinner("Calculating driving route..."):
            route = get_route(
                origin["lat"],
                origin["lon"],
                destination["lat"],
                destination["lon"],
            )

        if not route:
            st.error(
                "We could not calculate a driving route. "
                "Please try again."
            )
            return

        st.session_state["pending_ride"] = {
            "origin": origin,
            "destination": destination,
            "origin_address": origin_address,
            "destination_address": destination_address,
            "departure_date": departure_date.isoformat(),
            "departure_time": departure_time.strftime("%H:%M"),
            "available_seats": int(available_seats),
            "notes": notes,
            "route": route,
        }

    pending = st.session_state.get("pending_ride")

    if pending:
        st.subheader("Route Preview")

        c1, c2 = st.columns(2)
        c1.metric(
            "Route Distance",
            f"{pending['route']['distance_km']:.1f} km",
        )
        c2.metric(
            "Estimated Driving Time",
            f"{pending['route']['duration_min']:.0f} min",
        )

        fmap = create_route_map(
            pending["origin"],
            pending["destination"],
            pending["route"]["geometry"],
        )

        if fmap:
            st_folium(
                fmap,
                use_container_width=True,
                height=450,
                key="offer_route_map",
            )

        if st.button(
            "✓ Publish Ride",
            type="primary",
            use_container_width=True,
        ):
            execute(
                """
                INSERT INTO rides (
                    driver_id,
                    organization_id,
                    origin_address,
                    origin_lat,
                    origin_lon,
                    destination_address,
                    destination_lat,
                    destination_lon,
                    departure_date,
                    departure_time,
                    available_seats,
                    total_seats,
                    route_distance_km,
                    route_duration_min,
                    route_geometry,
                    notes,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    user["organization_id"],
                    pending["origin_address"],
                    pending["origin"]["lat"],
                    pending["origin"]["lon"],
                    pending["destination_address"],
                    pending["destination"]["lat"],
                    pending["destination"]["lon"],
                    pending["departure_date"],
                    pending["departure_time"],
                    pending["available_seats"],
                    pending["available_seats"],
                    pending["route"]["distance_km"],
                    pending["route"]["duration_min"],
                    json.dumps(pending["route"]["geometry"]),
                    pending["notes"],
                    "active",
                    now_iso(),
                ),
                commit=True,
            )

            st.session_state.pop("pending_ride", None)
            st.success("Ride successfully published!")
            st.rerun()


def page_find_ride():
    user = get_user(st.session_state["user_id"])

    st.title("🔎 Find a Ride")
    st.write(
        "The system looks for rides in your organization where your "
        "pickup point is close to the driver's actual road route."
    )

    with st.form("find_ride_form"):
        pickup_address = st.text_input(
            "Pickup Location",
            placeholder="G-11 Islamabad",
        )

        requested_date = st.date_input(
            "Date",
            min_value=date.today(),
            value=date.today() + timedelta(days=1),
        )

        requested_time = st.time_input(
            "Desired Time",
            value=time(8, 0),
        )

        requested_seats = st.number_input(
            "Number of Seats",
            min_value=1,
            max_value=10,
            value=1,
        )

        submitted = st.form_submit_button(
            "Find Matching Rides",
            use_container_width=True,
        )

    if submitted:
        if not pickup_address.strip():
            st.error("Please enter a pickup location.")
            return

        with st.spinner("Finding route-compatible rides..."):
            results, error = find_matches(
                user["organization_id"],
                pickup_address,
                requested_date,
                requested_time,
                int(requested_seats),
            )

        if error:
            st.error(error)
            return

        st.session_state["search_results"] = results
        st.session_state["search_request"] = {
            "pickup_address": pickup_address,
            "requested_date": requested_date.isoformat(),
            "requested_time": requested_time.strftime("%H:%M"),
            "requested_seats": int(requested_seats),
        }

    results = st.session_state.get("search_results")

    if results is None:
        return

    st.divider()

    if not results:
        st.info(
            "No strong matches found.\n\n"
            "Try a wider pickup area, a different departure time, "
            "or another date."
        )
        return

    st.subheader(f"{len(results)} matching ride(s)")

    for result in results:
        ride = result["ride"]
        match = result["match"]
        reasons = result["reasons"]

        rating = get_user_rating(ride["driver_id"])
        rating_text = f"{rating} ⭐" if rating else "No ratings yet"

        st.markdown(
            f"""
            <div class="ride-card">
                <div class="match-score">{match['score']}% Match</div>
                <b>Driver:</b> {ride['driver_name']}<br>
                <b>Rating:</b> {rating_text}<br>
                <b>From:</b> {ride['origin_address']}<br>
                <b>To:</b> {ride['destination_address']}<br>
                <b>Departure:</b> {ride['departure_date']} at {ride['departure_time']}<br>
                <b>Distance from route:</b> {result['distance_km']:.1f} km<br>
                <b>Seats:</b> {ride['available_seats']} available
            </div>
            """,
            unsafe_allow_html=True,
        )

        for reason in reasons:
            st.write(f"✓ {reason}")

        if st.button(
            "View Ride",
            key=f"view_ride_{ride['id']}",
            use_container_width=True,
        ):
            st.session_state["selected_ride"] = result
            st.session_state["page"] = "Ride Details"
            st.rerun()


def page_ride_details():
    result = st.session_state.get("selected_ride")

    if not result:
        st.info("No ride selected.")
        return

    ride = result["ride"]
    user = get_user(st.session_state["user_id"])

    st.title("🚗 Ride Details")

    match = result["match"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Match", f"{match['score']}%")
    c2.metric("Route Distance", f"{ride['route_distance_km']:.1f} km")
    c3.metric("Seats", ride["available_seats"])

    st.write(f"**Driver:** {ride['driver_name']}")
    st.write(f"**From:** {ride['origin_address']}")
    st.write(f"**To:** {ride['destination_address']}")
    st.write(
        f"**Departure:** {ride['departure_date']} at {ride['departure_time']}"
    )
    st.write(
        f"**Estimated driving time:** {ride['route_duration_min']:.0f} minutes"
    )

    st.subheader("Why this is a match")

    for reason in result["reasons"]:
        st.write(f"✓ {reason}")

    try:
        geometry = json.loads(ride["route_geometry"])

        origin = {
            "lat": ride["origin_lat"],
            "lon": ride["origin_lon"],
        }

        destination = {
            "lat": ride["destination_lat"],
            "lon": ride["destination_lon"],
        }

        fmap = create_route_map(
            origin,
            destination,
            geometry,
            result["pickup"],
        )

        if fmap:
            st_folium(
                fmap,
                use_container_width=True,
                height=500,
                key=f"details_map_{ride['id']}",
            )
    except (TypeError, json.JSONDecodeError):
        st.warning("Route map is unavailable.")

    existing = execute(
        """
        SELECT id, status
        FROM ride_requests
        WHERE ride_id = ? AND passenger_id = ?
          AND status = 'pending'
        """,
        (ride["id"], user["id"]),
        fetchone=True,
    )

    already_joined = execute(
        """
        SELECT id
        FROM ride_passengers
        WHERE ride_id = ? AND passenger_id = ?
          AND status = 'active'
        """,
        (ride["id"], user["id"]),
        fetchone=True,
    )

    if already_joined:
        st.success("You are already accepted for this ride.")
    elif existing:
        st.info("You already have a pending request for this ride.")
    elif ride["driver_id"] == user["id"]:
        st.info("You cannot request your own ride.")
    elif ride["available_seats"] <= 0:
        st.error("This ride is full.")
    else:
        if st.button(
            "Request Seat",
            type="primary",
            use_container_width=True,
        ):
            request_data = st.session_state.get("search_request")

            if not request_data:
                st.error("Search information is missing. Please search again.")
                return

            execute(
                """
                INSERT INTO ride_requests (
                    ride_id,
                    passenger_id,
                    pickup_address,
                    pickup_lat,
                    pickup_lon,
                    requested_date,
                    requested_time,
                    match_score,
                    match_reason,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    ride["id"],
                    user["id"],
                    request_data["pickup_address"],
                    result["pickup"]["lat"],
                    result["pickup"]["lon"],
                    request_data["requested_date"],
                    request_data["requested_time"],
                    match["score"],
                    " ".join(result["reasons"]),
                    now_iso(),
                ),
                commit=True,
            )

            create_notification(
                ride["driver_id"],
                "New ride request",
                f"{user['name']} requested a seat on your ride.",
            )

            st.success("Ride request sent successfully.")


def page_my_rides():
    user = get_user(st.session_state["user_id"])

    st.title("🧳 My Rides")

    st.subheader("Offered Rides")

    offered = get_active_rides_for_user(user["id"])

    if not offered:
        st.info("You have not offered any rides yet.")
    else:
        for ride in offered:
            with st.expander(
                f"{ride['departure_date']} {ride['departure_time']} — "
                f"{ride['origin_address']} → {ride['destination_address']}"
            ):
                st.write(f"Status: **{ride['status']}**")
                st.write(f"Seats: {ride['available_seats']}/{ride['total_seats']}")

                if ride["status"] == "active":
                    if st.button(
                        "Cancel Ride",
                        key=f"cancel_ride_{ride['id']}",
                    ):
                        execute(
                            """
                            UPDATE rides
                            SET status = 'cancelled'
                            WHERE id = ? AND driver_id = ?
                            """,
                            (ride["id"], user["id"]),
                            commit=True,
                        )

                        passengers = execute(
                            """
                            SELECT passenger_id
                            FROM ride_passengers
                            WHERE ride_id = ? AND status = 'active'
                            """,
                            (ride["id"],),
                            fetchall=True,
                        )

                        for passenger in passengers:
                            create_notification(
                                passenger["passenger_id"],
                                "Ride cancelled",
                                "A ride you joined has been cancelled.",
                            )

                        st.success("Ride cancelled.")
                        st.rerun()

    st.divider()

    st.subheader("Joined Rides")

    joined = execute(
        """
        SELECT
            r.*,
            u.name AS driver_name,
            rp.passenger_id
        FROM ride_passengers rp
        JOIN rides r ON r.id = rp.ride_id
        JOIN users u ON u.id = r.driver_id
        WHERE rp.passenger_id = ?
        ORDER BY r.departure_date, r.departure_time
        """,
        (user["id"],),
        fetchall=True,
    )

    if not joined:
        st.info("You have not joined any rides yet.")
    else:
        for ride in joined:
            st.write(
                f"**{ride['driver_name']}** — "
                f"{ride['origin_address']} → {ride['destination_address']} "
                f"on {ride['departure_date']} at {ride['departure_time']}"
            )


def page_requests():
    user = get_user(st.session_state["user_id"])

    st.title("📨 Ride Requests")

    requests = execute(
        """
        SELECT
            rr.*,
            r.driver_id,
            r.available_seats,
            r.status AS ride_status,
            u.name AS passenger_name
        FROM ride_requests rr
        JOIN rides r ON r.id = rr.ride_id
        JOIN users u ON u.id = rr.passenger_id
        WHERE r.driver_id = ?
          AND rr.status = 'pending'
        ORDER BY rr.created_at DESC
        """,
        (user["id"],),
        fetchall=True,
    )

    if not requests:
        st.info("No pending requests.")
        return

    for request in requests:
        st.markdown("---")
        st.write(f"**Passenger:** {request['passenger_name']}")
        st.write(f"**Pickup:** {request['pickup_address']}")
        st.write(f"**Requested Time:** {request['requested_time']}")
        st.write(f"**Match:** {request['match_score']:.0f}%")
        st.write(f"**Reason:** {request['match_reason']}")

        col1, col2 = st.columns(2)

        with col1:
            if st.button(
                "Accept",
                key=f"accept_{request['id']}",
                type="primary",
            ):
                if request["available_seats"] <= 0:
                    st.error("No seats remain.")
                    continue

                execute(
                    """
                    UPDATE ride_requests
                    SET status = 'accepted'
                    WHERE id = ? AND status = 'pending'
                    """,
                    (request["id"],),
                    commit=True,
                )

                try:
                    execute(
                        """
                        INSERT INTO ride_passengers (
                            ride_id,
                            passenger_id,
                            pickup_address,
                            pickup_lat,
                            pickup_lon,
                            status,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?, 'active', ?)
                        """,
                        (
                            request["ride_id"],
                            request["passenger_id"],
                            request["pickup_address"],
                            request["pickup_lat"],
                            request["pickup_lon"],
                            now_iso(),
                        ),
                        commit=True,
                    )

                    new_seats = request["available_seats"] - 1

                    execute(
                        """
                        UPDATE rides
                        SET available_seats = ?,
                            status = CASE
                                WHEN ? <= 0 THEN 'full'
                                ELSE 'active'
                            END
                        WHERE id = ?
                        """,
                        (new_seats, new_seats, request["ride_id"]),
                        commit=True,
                    )

                    create_notification(
                        request["passenger_id"],
                        "Ride request accepted",
                        "Your ride request was accepted.",
                    )

                    st.success("Request accepted.")
                    st.rerun()

                except sqlite3.IntegrityError:
                    st.error("This passenger is already on the ride.")

        with col2:
            if st.button(
                "Reject",
                key=f"reject_{request['id']}",
            ):
                execute(
                    """
                    UPDATE ride_requests
                    SET status = 'rejected'
                    WHERE id = ? AND status = 'pending'
                    """,
                    (request["id"],),
                    commit=True,
                )

                create_notification(
                    request["passenger_id"],
                    "Ride request rejected",
                    "Your ride request was rejected.",
                )

                st.success("Request rejected.")
                st.rerun()


def page_messages():
    user = get_user(st.session_state["user_id"])

    st.title("💬 Messages")

    rides = execute(
        """
        SELECT DISTINCT
            r.id,
            r.origin_address,
            r.destination_address,
            r.departure_date,
            r.departure_time,
            r.driver_id
        FROM rides r
        JOIN ride_passengers rp ON rp.ride_id = r.id
        WHERE rp.passenger_id = ?
          AND rp.status = 'active'
        """,
        (user["id"],),
        fetchall=True,
    )

    driver_rides = execute(
        """
        SELECT DISTINCT
            r.id,
            r.origin_address,
            r.destination_address,
            r.departure_date,
            r.departure_time,
            r.driver_id
        FROM rides r
        JOIN ride_passengers rp ON rp.ride_id = r.id
        WHERE r.driver_id = ?
          AND rp.status = 'active'
        """,
        (user["id"],),
        fetchall=True,
    )

    ride_map = {ride["id"]: ride for ride in rides}
    for ride in driver_rides:
        ride_map[ride["id"]] = ride

    if not ride_map:
        st.info("Messaging becomes available after a ride request is accepted.")
        return

    selected_id = st.selectbox(
        "Ride",
        list(ride_map.keys()),
        format_func=lambda rid: (
            f"Ride #{rid} — "
            f"{ride_map[rid]['origin_address']} → "
            f"{ride_map[rid]['destination_address']}"
        ),
    )

    ride = ride_map[selected_id]

    messages = execute(
        """
        SELECT
            m.*,
            u.name AS sender_name
        FROM messages m
        JOIN users u ON u.id = m.sender_id
        WHERE m.ride_id = ?
        ORDER BY m.created_at
        """,
        (selected_id,),
        fetchall=True,
    )

    for message in messages:
        label = "You" if message["sender_id"] == user["id"] else message["sender_name"]
        st.write(f"**{label}:** {message['message']}")

    passenger = execute(
        """
        SELECT passenger_id
        FROM ride_passengers
        WHERE ride_id = ? AND passenger_id != ?
          AND status = 'active'
        LIMIT 1
        """,
        (selected_id, user["id"]),
        fetchone=True,
    )

    if user["id"] == ride["driver_id"]:
        receiver = passenger["passenger_id"] if passenger else None
    else:
        receiver = ride["driver_id"]

    if receiver:
        with st.form("message_form"):
            message_text = st.text_input("Message")
            submitted = st.form_submit_button("Send Message")

        if submitted:
            if not message_text.strip():
                st.error("Message cannot be empty.")
            else:
                execute(
                    """
                    INSERT INTO messages
                    (ride_id, sender_id, receiver_id, message, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        selected_id,
                        user["id"],
                        receiver,
                        message_text.strip(),
                        now_iso(),
                    ),
                    commit=True,
                )

                create_notification(
                    receiver,
                    "New message",
                    f"{user['name']} sent you a message.",
                )

                st.rerun()


def page_notifications():
    user = get_user(st.session_state["user_id"])

    st.title("🔔 Notifications")

    notifications = execute(
        """
        SELECT *
        FROM notifications
        WHERE user_id = ?
        ORDER BY created_at DESC
        """,
        (user["id"],),
        fetchall=True,
    )

    if not notifications:
        st.info("No notifications.")
        return

    if st.button("Mark all as read"):
        execute(
            """
            UPDATE notifications
            SET is_read = 1
            WHERE user_id = ?
            """,
            (user["id"],),
            commit=True,
        )
        st.rerun()

    for notification in notifications:
        icon = "🔵" if not notification["is_read"] else "⚪"

        st.write(
            f"{icon} **{notification['title']}** — "
            f"{notification['message']}"
        )
        st.caption(notification["created_at"])


def page_admin():
    user = get_user(st.session_state["user_id"])

    if user["role"] != "admin":
        st.error("Admin access required.")
        return

    st.title("🛠️ Admin Dashboard")

    total_users = execute(
        "SELECT COUNT(*) AS count FROM users",
        fetchone=True,
    )["count"]

    total_orgs = execute(
        "SELECT COUNT(*) AS count FROM organizations",
        fetchone=True,
    )["count"]

    active_rides = execute(
        "SELECT COUNT(*) AS count FROM rides WHERE status = 'active'",
        fetchone=True,
    )["count"]

    pending_requests = execute(
        "SELECT COUNT(*) AS count FROM ride_requests WHERE status = 'pending'",
        fetchone=True,
    )["count"]

    reports = execute(
        "SELECT COUNT(*) AS count FROM reports WHERE status = 'Open'",
        fetchone=True,
    )["count"]

    c1, c2, c3, c4, c5 = st.columns(5)

    c1.metric("Users", total_users)
    c2.metric("Organizations", total_orgs)
    c3.metric("Active Rides", active_rides)
    c4.metric("Pending Requests", pending_requests)
    c5.metric("Open Reports", reports)

    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Users", "Organizations", "Rides", "Reports"]
    )

    with tab1:
        rows = execute(
            """
            SELECT
                u.id,
                u.name,
                u.email,
                o.name AS organization,
                u.role,
                u.is_verified,
                u.created_at
            FROM users u
            JOIN organizations o ON o.id = u.organization_id
            ORDER BY u.created_at DESC
            """,
            fetchall=True,
        )
        st.dataframe(pd.DataFrame([dict(r) for r in rows]))

    with tab2:
        rows = execute(
            "SELECT * FROM organizations ORDER BY name",
            fetchall=True,
        )
        st.dataframe(pd.DataFrame([dict(r) for r in rows]))

    with tab3:
        rows = execute(
            """
            SELECT
                r.id,
                u.name AS driver,
                o.name AS organization,
                r.origin_address,
                r.destination_address,
                r.departure_date,
                r.departure_time,
                r.available_seats,
                r.status
            FROM rides r
            JOIN users u ON u.id = r.driver_id
            JOIN organizations o ON o.id = r.organization_id
            ORDER BY r.created_at DESC
            """,
            fetchall=True,
        )
        st.dataframe(pd.DataFrame([dict(r) for r in rows]))

    with tab4:
        rows = execute(
            """
            SELECT
                rep.id,
                reporter.name AS reporter,
                reported.name AS reported,
                rep.reason,
                rep.description,
                rep.status,
                rep.created_at
            FROM reports rep
            JOIN users reporter ON reporter.id = rep.reporter_id
            JOIN users reported ON reported.id = rep.reported_user_id
            ORDER BY rep.created_at DESC
            """,
            fetchall=True,
        )

        if rows:
            st.dataframe(pd.DataFrame([dict(r) for r in rows]))

            report_ids = [r["id"] for r in rows]

            selected_report = st.selectbox(
                "Select report",
                report_ids,
            )

            new_status = st.selectbox(
                "New status",
                ["Open", "Reviewed", "Resolved"],
            )

            if st.button("Update Report"):
                execute(
                    """
                    UPDATE reports
                    SET status = ?
                    WHERE id = ?
                    """,
                    (new_status, selected_report),
                    commit=True,
                )
                st.success("Report updated.")
                st.rerun()
        else:
            st.info("No reports.")


def page_reports_and_safety():
    user = get_user(st.session_state["user_id"])

    st.title("🛡️ Safety")

    st.subheader("Report a User")

    users = execute(
        """
        SELECT id, name
        FROM users
        WHERE id != ?
        ORDER BY name
        """,
        (user["id"],),
        fetchall=True,
    )

    if users:
        user_options = {u["name"]: u["id"] for u in users}

        with st.form("report_form"):
            reported_name = st.selectbox(
                "User",
                list(user_options.keys()),
            )
            reason = st.selectbox(
                "Reason",
                [
                    "Inappropriate behavior",
                    "Unsafe driving",
                    "No-show",
                    "Harassment",
                    "Other",
                ],
            )
            description = st.text_area("Description")
            submitted = st.form_submit_button("Submit Report")

        if submitted:
            execute(
                """
                INSERT INTO reports
                (reporter_id, reported_user_id, reason, description, status, created_at)
                VALUES (?, ?, ?, ?, 'Open', ?)
                """,
                (
                    user["id"],
                    user_options[reported_name],
                    reason,
                    description,
                    now_iso(),
                ),
                commit=True,
            )

            st.success("Report submitted.")

    st.divider()

    st.subheader("Block a User")

    if users:
        block_name = st.selectbox(
            "User to block",
            list(user_options.keys()),
            key="block_user",
        )

        if st.button("Block User"):
            try:
                execute(
                    """
                    INSERT INTO blocked_users
                    (blocker_id, blocked_user_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        user["id"],
                        user_options[block_name],
                        now_iso(),
                    ),
                    commit=True,
                )
                st.success(f"{block_name} has been blocked.")
            except sqlite3.IntegrityError:
                st.info("That user is already blocked.")


# ============================================================
# MAIN APPLICATION
# ============================================================

init_db()

if "page" not in st.session_state:
    st.session_state["page"] = "Home"

if not is_logged_in():
    page_auth()
else:
    user = get_user(st.session_state["user_id"])

    unread_count = len(get_unread_notifications(user["id"]))

    pages = [
        "Home",
        "Profile",
        "Offer a Ride",
        "Find a Ride",
        "Ride Details",
        "My Rides",
        "Ride Requests",
        "Messages",
        f"Notifications ({unread_count})",
        "Safety",
    ]

    if user["role"] == "admin":
        pages.append("Admin Dashboard")

    current_page = st.sidebar.radio(
        "Navigation",
        pages,
        index=(
            pages.index(st.session_state["page"])
            if st.session_state["page"] in pages
            else 0
        ),
    )

    if current_page.startswith("Notifications"):
        current_page = "Notifications"

    st.session_state["page"] = current_page

    st.sidebar.divider()
    st.sidebar.write(f"👤 **{user['name']}**")
    st.sidebar.write(f"🏢 {user['organization_name']}")

    if st.sidebar.button("Logout", use_container_width=True):
        logout()
        st.rerun()

    if current_page == "Home":
        page_home()
    elif current_page == "Profile":
        page_profile()
    elif current_page == "Offer a Ride":
        page_offer_ride()
    elif current_page == "Find a Ride":
        page_find_ride()
    elif current_page == "Ride Details":
        page_ride_details()
    elif current_page == "My Rides":
        page_my_rides()
    elif current_page == "Ride Requests":
        page_requests()
    elif current_page == "Messages":
        page_messages()
    elif current_page == "Notifications":
        page_notifications()
    elif current_page == "Safety":
        page_reports_and_safety()
    elif current_page == "Admin Dashboard":
        page_admin()
