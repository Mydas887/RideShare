
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
