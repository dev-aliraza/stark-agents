import json, re

class Util:
    
    @classmethod
    def load_json(cls, json_string):
        # Cleaning Json String if there are back ticks or `json` keyword
        cleaned_string = re.sub(r'^```*|```$', '', json_string.strip(), flags=re.MULTILINE)
        cleaned_string = re.sub(r'^json*|$', '', cleaned_string.strip(), flags=re.MULTILINE)
        cleaned_string = cleaned_string.strip()
        
        try:
            # Converting cleaned JSON string to python `dict`
            return json.loads(cleaned_string)
        except json.JSONDecodeError as e:
            return f"Error parsing JSON: {e}"