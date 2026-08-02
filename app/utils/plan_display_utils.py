"""Presentation-only formatting for persisted subscription plan identifiers."""


def display_plan_name(plan_name):
    """Return a customer-facing plan name without changing its stored value."""
    if isinstance(plan_name, str) and plan_name.strip().casefold() == "starter":
        return "Premium"
    return plan_name


def register_plan_display_filter(app):
    app.jinja_env.filters["display_plan_name"] = display_plan_name
