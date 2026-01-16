import re, runpy
from pathlib import Path
from typing import List, Dict, Any
from .agent import Agent
from .tools import CodeTool

class Skill:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.metadata_as_tools = []
        self.skills: Dict[str, Dict] = {}
        self.__load_skills_data()

    def __parse_skill_file(self, file_path: Path) -> Dict[str, Any]:
        """
        Parse a SKILL.md file and extract metadata and content.
        
        Args:
            file_path: Path to the SKILL.md file
            
        Returns:
            Dictionary containing skill metadata and content
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Pattern to match YAML frontmatter between ---
        frontmatter_pattern = r'^---\s*\n(.*?)\n---\s*\n(.*)$'
        match = re.match(frontmatter_pattern, content, re.DOTALL)
        
        if not match:
            # No frontmatter found, return content as skill
            return {
                'skill': content.strip(),
                'file_path': str(file_path)
            }
        
        frontmatter_text = match.group(1)
        skill_content = match.group(2).strip()
        
        # Parse frontmatter (simple YAML parsing for key: value pairs)
        skill_data = {}
        for line in frontmatter_text.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # Match key: value pattern
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()
                skill_data[key] = value
        
        # Add the skill content
        skill_data['skill'] = skill_content
        skill_data['file_path'] = str(file_path)
        
        return skill_data

    def __load_skills_data(self) -> List[Dict[str, Any]]:
        """
        Traverse all folders and sub-folders on the given path,
        find SKILL.md files, parse their metadata and content,
        and return them as a list of dictionaries.
        
        Args:
            path: The root directory path to search for SKILL.md files
            
        Returns:
            List of dictionaries containing skill metadata and content
        """
        for skill_path in self.agent.get_skills():
            # Convert to Path object for easier manipulation
            root_path = Path(skill_path)
            
            # Check if path exists
            if not root_path.exists():
                raise ValueError(f"Path does not exist: {skill_path}")
            
            # Recursively find all SKILL.md files
            for skill_file in root_path.rglob("SKILL.md"):
                try:
                    skill_data = self.__parse_skill_file(skill_file)
                    if skill_data:
                        skill_name = "skill___" + skill_data["name"]
                        self.metadata_as_tools.append({
                            "type": "function",
                            "function": {
                                "name": skill_name,
                                "description": skill_data["description"]
                            }
                        })
                        self.skills[skill_name] = {
                            "skill": skill_data["skill"],
                            "skill_path": Path(skill_data["file_path"]).parent.as_posix()
                        }
                except Exception as e:
                    # Log error but continue processing other files
                    print(f"Exception in {skill_file}: {e}")
    
    def get_metadata_as_tools(self) -> List[Dict[str, Any]]:
        return self.metadata_as_tools
    
    def get_skills(self) -> Dict[str, Dict]:
        return self.skills

    def is_skill(self, name):
        if name in self.skills:
            return True
        return False

    def get_skill_subagent(self, tool_name) -> Agent:
        skill = self.skills[tool_name].get("skill")
        skill_path = self.skills[tool_name].get("skill_path")
        instructions = f"""This is a skill. Skill folder path is '{skill_path}'.
---
{skill}
"""
        code_tool = CodeTool()
        mcp_servers = {}
        function_tools = []
        if Path(skill_path + "/tools.py").exists():
            tools_var = runpy.run_path(skill_path + "/tools.py")
            if "TOOLS" in tools_var:
                tools: dict = tools_var.get("TOOLS")
                mcp_servers = tools.get("mcp", {})
                function_tools = tools.get("function", [])
        return Agent(
            name=tool_name,
            instructions=instructions,
            model=self.agent.get_skill_model() if self.agent.get_skill_model() else self.agent.get_model(),
            mcp_servers=mcp_servers,
            function_tools=[code_tool] + function_tools,
            max_output_tokens=64000,
            parallel_tool_calls=True,
            max_iterations=100
        )