import webbrowser
import urllib.parse
import logging

log = logging.getLogger(__name__)


def execute(args: dict) -> str:
    action = args.get("action", "")
    url = args.get("url", "")
    query = args.get("query", "")

    try:
        if action == "open_url":
            if not url:
                return "Error: No URL provided."
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            webbrowser.open(url)
            return f"Opened {url}"

        elif action == "search":
            if not query:
                return "Error: No search query."
            encoded = urllib.parse.quote(query)
            search_url = f"https://www.google.com/search?q={encoded}"
            webbrowser.open(search_url)
            return f"Searching Google for: {query}"

        elif action == "youtube":
            if query:
                encoded = urllib.parse.quote(query)
                webbrowser.open(
                    f"https://www.youtube.com/results?search_query={encoded}")
                return f"Searching YouTube for: {query}"
            else:
                webbrowser.open("https://www.youtube.com")
                return "Opened YouTube."

        elif action == "github":
            if query:
                encoded = urllib.parse.quote(query)
                webbrowser.open(f"https://github.com/search?q={encoded}")
                return f"Searching GitHub for: {query}"
            else:
                webbrowser.open("https://github.com")
                return "Opened GitHub."

        elif action == "maps":
            if query:
                encoded = urllib.parse.quote(query)
                webbrowser.open(
                    f"https://www.google.com/maps/search/{encoded}")
                return f"Searching maps for: {query}"
            else:
                webbrowser.open("https://www.google.com/maps")
                return "Opened Google Maps."

        elif action == "twitter":
            if query:
                encoded = urllib.parse.quote(query)
                webbrowser.open(f"https://twitter.com/search?q={encoded}")
                return f"Searching Twitter for: {query}"
            else:
                webbrowser.open("https://twitter.com")
                return "Opened Twitter."

        elif action == "new_tab":
            webbrowser.open("about:blank")
            return "Opened new tab."

        elif action == "go_back":
            # Not directly supported by webbrowser module
            return "Browser back navigation not implemented. Use keyboard shortcut."

        elif action == "go_forward":
            return "Browser forward navigation not implemented."

        elif action == "refresh":
            return "Browser refresh not implemented. Use keyboard shortcut."

        elif action == "close_tab":
            return "Tab close not implemented via webbrowser module."

        elif action == "download":
            if not url:
                return "Error: No download URL."
            webbrowser.open(url)
            return f"Initiated download from {url}"

        else:
            return f"Unknown browser action: {action}"

    except Exception as e:
        log.error(f"Browser control failed: {e}")
        return f"Error: {str(e)}"
