import os
import json

class Scorekeeper:

    """==========================================================================
    Made by Kate Kiefer, February 2022
    Updated May 2022 to support non-integer score values.
    Manages all the scoreboards as dict data structures.
    All scoreboards reserve the "__saved__" key to mark whether or not the individual scoreboard matches the version on disk.
    When Scorekeeper.is_saved==False, at least one of the scoreboards in memory no longer matches the version saved to disk.
    Use Scorekeeper.save() and Scorekeeper.load() to save and load contents to and from the disk as necessary.
    Sorting, getting top scores, and all that junk must be done externally.
    =============================================================================
    """

    default_path=os.path.abspath("misc_data/scoreboards/")+"/"

    def __init__(self, default_path=os.path.abspath("misc_data/scoreboards/")+"/"):
        """
        Loads all scoreboards from the default_path directory.
        Uses the default default_path if not specified.
        """
        #print("INITING")
        self.default_path = default_path
        self.scoreboards = {}
        self.load()
        #print(self.scoreboards)
        #print("DONE")


    def change_score(self, key, scoreboard_name, value=1, increment=True):
        """
        Changes a scoreboard value for a given scoreboard and key.
        key and scoreboard_name are both strings.
        increment specifies whether or not value should add to or replace the current score.
        Raises TypeError if increment=True and the variable type cannot be added (+) with the value saved. (I think that makes sense)
        If no score is set for a given key, the new score will be added to the dictionary.
        returns the new value of the scoreboard
        """
        #print("CHANGING SCOREBOARD: "+scoreboard_name+" of "+str(key)+" by "+str(number)+("inc" if increment else ""))

        #If increment=True, make sure the value is an additive type.
        if increment:
            try:
                testvalue = value+value #I suppose this is one way to do this
            except:
                raise TypeError("The passed-in value is not additive (type: "+str(type(value))+"). It cannot be used when increment=True")

        key = str(key)
        #reserved __saved__ marker
        if key == "__saved__":
            raise ValueError("The __saved__ key is reserved, and should not be modified this way.")

        try: #get the scoreboard dict
            scoreboard = self.scoreboards[scoreboard_name]
        except KeyError: #scoreboard does not exist yet - create a new one with the new value
            scoreboard = self.add_scoreboard(scoreboard_name, new_scoreboard={key:value})
            return value

        try: #get the old_score and overwrite/increment as needed
            old_score = scoreboard[key]

            if increment: #try to increment
                try:
                    new_score = old_score+value
                except:
                    raise TypeError("The given value (type: "+str(type(value))+") could not be added to the original value (type: "+str(type(old_score))+") (incompatable types probably)")
            else:
                new_score = value

            if old_score != new_score: #Dont bother saving if nothing has changed
                scoreboard[key]=new_score
                scoreboard["__saved__"]=0
                self.is_saved = False

        except KeyError: #the given key has no current score - create the new score
            new_score = value
            scoreboard[key]=new_score
            scoreboard["__saved__"]=0
            self.is_saved = False
        return new_score
        

    def set_score(self, key, scoreboard_name, value):
        """
        Wrapper for change_score that sets increment=False
        """
        return self.change_score(key, scoreboard_name, value=value, increment=False)


    def get_score(self, key, scoreboard_name, show_none=True):
        """
        Retrieves the scoreboard value for a given scoreboard and key (both strings).
        If no score is currently set, returns None. If show_none=False, will return 0 instead.
        The scoreboard will not be modified in any way by this function.
        """

        key = str(key)
        #reserved __saved__ marker
        if key == "__saved__":
            raise ValueError("The __saved__ key is reserved, and should not be accessed this way.")

        try:
            scoreboard = self.scoreboards[scoreboard_name]
            return scoreboard[key]
        except KeyError:
            return None if show_none else 0


    def get_scoreboard(self, scoreboard_name, show_none=True):
        """
        Returns the entire scoreboard dictionary object for external processing.
        If the specified scoreboard does not exist, it will return None. If show_none=False, it will return an empty dict instead.
        Inportant Note - External processing should not modify the scoreboard in any way as it will not properly update the "is saved" markers for the scoreboard.
        """
        try:
            return self.scoreboards[scoreboard_name]
        except KeyError:
            return None if show_none else {}


    def add_scoreboard(self, scoreboard_name, new_scoreboard={}, overwrite=False):
        """
        Creates a new scoreboard object and adds it to the scoreboard dictionary.
        If new_scoreboard is specified, the new scoreboard will inherit its values.
        Raises ValueError if a scoreboard already exists with the same name and overwrite=False.
        Overwrites an existing scoreboard if overwrite=True.
        """

        #print("+ADDING SCOREBOARD: "+scoreboard_name)
        try:
            board = self.scoreboards[scoreboard_name]
            if not overwrite:
                raise ValueError("That scoreboard already exists! Cannot add it without overwriting the existing one: "+scoreboard_name)
            #print("OVERWRITING")
            board.clear()
            board["__saved__"]=0
            board.update(new_scoreboard)
            self.is_saved = False
            returned = board
        except KeyError:
            #print("HERE")
            #print("++CURRENTLY: "+str(new_scoreboard))
            new_scoreboard["__saved__"]=0
            self.scoreboards[scoreboard_name]=new_scoreboard
            self.is_saved = False
            #print(self.scoreboards[scoreboard_name])
            returned = new_scoreboard
        #print("++RETURNING: "+str(returned))
        return returned


    def save(self, path=default_path, force_all=False):
        """
        Runs through all the scoreboard dicitonaries in memory and saves them as .jsons to the specified path (uses defualt_path if unspecified).
        Only saves the scoreboards tagged as updated/unsaved ("__saved__" : 0) to save on write time.
        Will overwrite any old scoreboard files in the process.
        """
        #print("=SAVING")

        boards = [(k,v) for k,v in self.scoreboards.items()] #convert into list of tuples for iterating
        if not force_all:
            boards = list(filter(lambda x: x[1].get("__saved__")==0 or not x[1].get("__saved__"), boards)) #filter out boards that don't need saving

        for board in boards:
            #print("==SAVING: "+board[0]+" : "+str(board[1]))
            with open(path+board[0]+".json", "w") as f:
                json.dump(board[1], f)
            board[1]["__saved__"]=1 #mark as saved
        self.is_saved = True


    def load(self, path=default_path, clear_old=True):
        """
        Loads all .jsons in the specified path (uses default_path if unspecified) into memory.
        If clear_old=True, the current scoreboards will be flushed beforehand.
        If clear_old=False, only those scoreboards in memory with names matching the json files will be overwritten.
        """
        #print("#LOADING")
        if clear_old:
            self.scoreboards.clear()
        file_list = os.listdir(path)
        for file in file_list:
            if not file.endswith(".json") or file.startswith("."): #ignore hidden files and non-jsons
                continue
            #print("##LOADING FILE: "+file)
            with open(path+file, "r") as f:
                board = json.load(f)

            loaded = self.add_scoreboard(file[:-5], new_scoreboard=board, overwrite=True)
            loaded["__saved__"]=1
        self.is_saved = True
                

    def __iter__(self, scoreboard_name = None):
        """
        Included for iterabel support - Not really sure if it works.
        """
        if scoreboard_name:
            return self.get_scoreboard(scoreboard_name)
        return self.scoreboards


    