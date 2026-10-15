"""CLI entrypoint for the shared earnings forecast calculation."""

from researchx.skills._shared.forecasting.forecast import (
    calculate_forecast as calculate_forecast,
    project_year as project_year,
    REQUIRED_ASSUMPTIONS as REQUIRED_ASSUMPTIONS,
    SECTION_KEYS as SECTION_KEYS,
    main,
)

if __name__ == "__main__":
    main()
