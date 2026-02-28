"""
Control Rules Module

This module implements the management of control rules in the SWMM model.
Control rules determine how pumps and flow regulators are operated over the course of a simulation.
"""
from .utils import *
import logging

logger = logging.getLogger(__name__)


class ControlRule:
    """
    Represents a single control rule.
    
    A control rule consists of a name, a set of conditions, and a set of actions.
    Format:
    RULE ruleID
    IF condition_1
    AND condition_2
    OR condition_3
    THEN action_1
    AND action_2
    ELSE action_3
    PRIORITY value
    """
    def __init__(self, name):
        self.name = name
        self.priority = None
        self.conditions = []  # List of condition strings (e.g. "NODE N23 DEPTH > 10")
        self.actions = []     # List of action strings (e.g. "PUMP P45 STATUS = OFF")
        self.else_actions = [] # List of else action strings

    def __repr__(self):
        return f'Rule <{self.name}>'


class ControlList:
    """
    A collection class for managing control rules.
    """
    def __init__(self):
        self.data = []

    def __repr__(self):
        return f'{len(self.data)} Control Rules'

    def __len__(self):
        return len(self.data)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.data[key]
        elif isinstance(key, str):
            for item in self.data:
                if item.name == key:
                    return item
            raise KeyError(f"No rule found with name '{key}'")
        else:
            raise TypeError("Key must be an integer or a string")

    def __iter__(self):
        return iter(self.data)

    def add_rule(self, rule):
        """
        Add a control rule to the list.
        
        Args:
            rule (ControlRule): The rule object to add
        """
        if any(r.name == rule.name for r in self.data):
            raise ValueError(f"Rule with name '{rule.name}' already exists")
        self.data.append(rule)

    def read_from_swmm_inp(self, filename):
        """
        Read control rules from a SWMM input file.
        
        Args:
            filename (str): Path to the SWMM input file
        """
        contents = get_swmm_inp_content(filename, '[CONTROLS]')
        
        current_rule = None
        current_section = None # 'IF', 'THEN', 'ELSE'
        
        for line in contents:
            line = line.strip()
            if not line:
                continue
                
            parts = line.split()
            keyword = parts[0].upper()
            
            if keyword == 'RULE':
                # Start new rule
                rule_name = parts[1]
                current_rule = ControlRule(rule_name)
                self.data.append(current_rule)
                current_section = None
                
            elif keyword == 'IF':
                current_section = 'IF'
                if current_rule:
                    # Store the whole line except 'IF '
                    current_rule.conditions.append(' '.join(parts[1:]))
                    
            elif keyword == 'THEN':
                current_section = 'THEN'
                if current_rule:
                    current_rule.actions.append(' '.join(parts[1:]))
                    
            elif keyword == 'ELSE':
                current_section = 'ELSE'
                if current_rule:
                    current_rule.else_actions.append(' '.join(parts[1:]))
                    
            elif keyword == 'PRIORITY':
                if current_rule:
                    current_rule.priority = parts[1]
                    
            elif keyword == 'AND' or keyword == 'OR':
                if current_rule and current_section:
                    # Append logic operator and the rest of the condition/action
                    content = ' '.join(parts)
                    if current_section == 'IF':
                        current_rule.conditions.append(content)
                    elif current_section == 'THEN':
                        current_rule.actions.append(content)
                    elif current_section == 'ELSE':
                        current_rule.else_actions.append(content)
            else:
                # Handle continuation or un-prefixed lines if any (though standard format usually has prefixes)
                # Assuming standard format for now.
                pass

    def write_to_swmm_inp(self, filename):
        """
        Write control rules to a SWMM input file.
        
        Args:
            filename (str): Path to the SWMM input file
        """
        try:
            with open(filename, 'a', encoding='utf-8') as f:
                f.write('\n\n[CONTROLS]\n')
                for rule in self.data:
                    f.write(f'RULE {rule.name}\n')
                    
                    if rule.conditions:
                        f.write(f'IF {rule.conditions[0]}\n')
                        for cond in rule.conditions[1:]:
                            f.write(f'{cond}\n')
                            
                    if rule.actions:
                        f.write(f'THEN {rule.actions[0]}\n')
                        for action in rule.actions[1:]:
                            f.write(f'{action}\n')
                            
                    if rule.else_actions:
                        f.write(f'ELSE {rule.else_actions[0]}\n')
                        for action in rule.else_actions[1:]:
                            f.write(f'{action}\n')
                            
                    if rule.priority:
                        f.write(f'PRIORITY {rule.priority}\n')
                    
                    f.write('\n')
            return 0
        except IOError as e:
            raise IOError(f"Error writing to SWMM input file: {str(e)}")
