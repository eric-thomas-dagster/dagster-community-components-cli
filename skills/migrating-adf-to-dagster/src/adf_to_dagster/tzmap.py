"""Windows time-zone names (what ADF triggers and the time-zone functions use) to IANA names. Well-known CLDR pairs; an unknown name is an error, never a guess."""

WINDOWS_TZ = {
    "UTC": "UTC", "Greenwich Standard Time": "Atlantic/Reykjavik", "GMT Standard Time": "Europe/London",
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago", "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles", "US Mountain Standard Time": "America/Phoenix", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "Atlantic Standard Time": "America/Halifax", "Newfoundland Standard Time": "America/St_Johns",
    "Canada Central Standard Time": "America/Regina", "Central America Standard Time": "America/Guatemala", "Mexico Standard Time": "America/Mexico_City",
    "SA Pacific Standard Time": "America/Bogota", "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "W. Europe Standard Time": "Europe/Berlin", "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris", "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest", "Russian Standard Time": "Europe/Moscow", "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem", "Arab Standard Time": "Asia/Riyadh", "Arabian Standard Time": "Asia/Dubai",
    "South Africa Standard Time": "Africa/Johannesburg", "Egypt Standard Time": "Africa/Cairo", "W. Central Africa Standard Time": "Africa/Lagos",
    "E. Africa Standard Time": "Africa/Nairobi", "India Standard Time": "Asia/Kolkata", "Pakistan Standard Time": "Asia/Karachi",
    "Bangladesh Standard Time": "Asia/Dhaka", "SE Asia Standard Time": "Asia/Bangkok", "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore", "Taipei Standard Time": "Asia/Taipei", "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul", "AUS Eastern Standard Time": "Australia/Sydney", "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Central Standard Time": "Australia/Darwin", "Cen. Australia Standard Time": "Australia/Adelaide", "W. Australia Standard Time": "Australia/Perth",
    "New Zealand Standard Time": "Pacific/Auckland",
}
