import dataclasses
import sqlite3
import uuid
import databases
import toml
import json
import itertools
import requests
import httpx
import os
import socket
from quart import Quart, abort, g, request
from quart_schema import QuartSchema, validate_request

app = Quart(__name__)
QuartSchema(app)

app.config.from_file(f"./etc/{__name__}.toml", toml.load)


@dataclasses.dataclass
class Game:
    username: str

@dataclasses.dataclass
class Url:
    url: str

@dataclasses.dataclass
class Guess:
    gameid: str
    word: str

@dataclasses.dataclass
class Information:
    user: str

database_list = ['DATABASE_PRIMARY','DATABASE_SECONDARY1','DATABASE_SECONDARY2']
database_index = itertools.cycle(database_list)

async def _connect_db():
    database = databases.Database(app.config[next(database_index)]["URL"])
    await database.connect()
    return database

async def _connect_db_primary():
    database = databases.Database(app.config["DATABASE_PRIMARY"]["URL"])
    await database.connect()
    return database
#make _get_db_primary function
def _get_db():
    if not hasattr(g, "sqlite_db"):
        g.sqlite_db = _connect_db()
    return g.sqlite_db

def _get_db_primary():
    if not hasattr(g, "sqlite_db_primary"):
        g.sqlite_db_primary = _connect_db_primary()
    return g.sqlite_db_primary


@app.teardown_appcontext
async def close_connection(exception):
    db = getattr(g, "_sqlite_db", None)
    if db is not None:
        await db.disconnect()


@app.route("/newgame", methods=["POST"])
async def create_game():
    # auth method referenced from https://www.youtube.com/watch?v=VW8qJxy4XcQ
    auth = request.authorization
    if auth and auth.username and auth.password:
        db = await _get_db()
        db_primary = await _get_db_primary()

        # Retrive random ID from the answers table
        word = await db.fetch_one(
            "SELECT answerid FROM answer ORDER BY RANDOM() LIMIT 1"
        )

        # Check if the retrived word is a repeat for the user, and if so grab a new word
        print(auth.username)
        print(word)
        while await db.fetch_one(
            "SELECT answerid FROM games WHERE username = :username AND answerid = :answerid",
            values={"username": auth.username, "answerid": word[0]},
        ):
            word = await db.fetch_one(
                "SELECT answerid FROM answer ORDER BY RANDOM() LIMIT 1"
            )

        # Create new game with 0 guesses
        gameid = str(uuid.uuid1())
        values = {"gameid": gameid, "guesses": 0, "gstate": "In-progress"}
        await db_primary.execute(
            "INSERT INTO game(gameid, guesses, gstate) VALUES(:gameid, :guesses, :gstate)",
            values,
        )

        # Create new row into Games table which connect with the recently connected game
        values = {"username": auth.username, "answerid": word[0], "gameid": gameid}
        await db_primary.execute(
            "INSERT INTO games(username, answerid, gameid) VALUES(:username, :answerid, :gameid)",
            values,
        )

        return values, 201
    else:
        return (
            {"error": "User not verified"},
            401,
            {"WWW-Authenticate": 'Basic realm = "Login required"'},
        )


# Should validate to check if guess is in valid_word table
# if it is then insert into guess table
# update game table by decrementing guess variable
# if word is not valid throw 404 exception
@app.route("/addguess", methods=["PUT"])
@validate_request(Guess)
async def add_guess(data):
    # auth method referenced from https://www.youtube.com/watch?v=VW8qJxy4XcQ
    auth = request.authorization
    if auth and auth.username and auth.password:
        db = await _get_db()
        db_primary = await _get_db_primary()
        currGame = dataclasses.asdict(data)

        # checks whether guessed word is the answer for that game
        isAnswer = await db.fetch_one(
            "SELECT * FROM answer as a where (select count(*) from games where gameid = :gameid and answerid = a.answerid)>=1 and a.answord = :word;",
            currGame,
        )

        # is guessed word the answer
        if isAnswer is not None and len(isAnswer) >= 1:
            # update game status
            try:
                await db_primary.execute(
                    """
                    UPDATE game set gstate = :status where gameid = :gameid
                    """,
                    values={"status": "Finished", "gameid": currGame["gameid"]},
                )
            except sqlite3.IntegrityError as e:
                abort(404, e)

            webhook_url = 'http://127.0.0.1:5500/payload'
            numguesses = await db.fetch_one(
                "SELECT guesses FROM game WHERE gameid = :gameid",
                values={"gameid":currGame["gameid"]}
            )
            # print(numguesses[0])20:35:09 primary.1     | HOopla

            print("HOla")
            return {
                "guessedWord": currGame["word"],
                "Accuracy": "\u2713" * 5,
            }, 201  # should return correct answer?

        # if 1 then word is valid otherwise it isn't valid and also check if they exceed guess limit
        isValidGuess = await db.fetch_one(
            "SELECT * from valid_word where valword = :word;",
            values={"word": currGame["word"]},
        )
        if isValidGuess is None:
            isValidGuess = await db.fetch_one(
                "SELECT * from answer where answord = :word;",
                values={"word": currGame["word"]},
            )

        guessNum = await db.fetch_one(
            "SELECT guesses from game where gameid = :gameid",
            values={"gameid": currGame["gameid"]},
        )
        accuracy = ""

        if isValidGuess is not None and len(isValidGuess) >= 1 and guessNum[0] < 6:
            try:
                # make a dict mapping each character and its position from the answer
                answord = await db.fetch_one(
                    "SELECT answord FROM answer as a, games as g  where g.gameid = :gameid and g.answerid = a.answerid",
                    values={"gameid": currGame["gameid"]},
                )

                ansDict = {}
                for i in range(len(answord[0])):
                    ansDict[answord[0][i]] = i

                # compare location of guessed word with answer
                guess_word = currGame["word"]
                for i in range(len(guess_word)):
                    if guess_word[i] in ansDict:
                        # print(ansDict.get(guess_word[i]))
                        if ansDict.get(guess_word[i]) == i:
                            accuracy += "\u2713"
                        else:
                            accuracy += "O"
                    else:
                        accuracy += "X"

                # insert guess word into guess table with accruracy
                await db_primary.execute(
                    "INSERT INTO guess(gameid,guessedword, accuracy) VALUES(:gameid, :guessedword, :accuracy)",
                    values={
                        "guessedword": currGame["word"],
                        "gameid": currGame["gameid"],
                        "accuracy": accuracy,
                    },
                )
                # update game table's guess variable by decrementing it
                await db_primary.execute(
                    """
                    UPDATE game set guesses = :guessNum where gameid = :gameid
                    """,
                    values={
                        "guessNum": (guessNum[0] + 1),
                        "gameid": currGame["gameid"],
                    },
                )

                # if after updating game number of guesses reaches max guesses then mark game as finished
                if guessNum[0] + 1 >= 6:
                    # update game status as finished
                    await db_primary.execute(
                        """
                        UPDATE game set gstate = :status where gameid = :gameid
                        """,
                        values={"status": "Finished", "gameid": currGame["gameid"]},
                    )
                    return "Max attempts.", 202
            except sqlite3.IntegrityError as e:
                abort(404, e)
        else:
            # should return msg saying invalid word?
            return {"Error": "Invalid Word"}

        return {"guessedWord": currGame["word"], "Accuracy": accuracy}, 201
    else:
        return (
            {"error": "User not verified"},
            401,
            {"WWW-Authenticate": 'Basic realm = "Login required"'},
        )


@app.route("/allgames", methods=["GET"])
async def all_games():
    # auth method referenced from https://www.youtube.com/watch?v=VW8qJxy4XcQ
    auth = request.authorization
    if auth and auth.username and auth.password:
        db = await _get_db()

        games_val = await db.fetch_all(
            "SELECT * FROM game as a where gameid IN (select gameid from games where username = :username) and a.gstate = :gstate;",
            values={"username": auth.username, "gstate": "In-progress"},
        )

        if games_val is None or len(games_val) == 0:
            return {"Message": "No Active Games"}, 406

        return list(map(dict, games_val))
    else:
        return (
            {"error": "User not verified"},
            401,
            {"WWW-Authenticate": 'Basic realm = "Login required"'},
        )


@app.route("/onegame", methods=["GET"])
async def my_game():
    # auth method referenced from https://www.youtube.com/watch?v=VW8qJxy4XcQ
    auth = request.authorization
    if auth and auth.username and auth.password:
        db = await _get_db()
        gameid = request.args.get("id")

        results = await db.fetch_all(
            "select * from game where gameid = :gameid",
            values={"gameid": gameid},
        )

        guess = await db.fetch_all(
            "select guessedword, accuracy from guess where gameid = :gameid",
            values={"gameid": gameid},
        )

        if results[0][2] == "Finished":
            return {"Message": "Not An Active Game"}, 406
        return list(map(dict, (results + guess)))
    else:
        return (
            {"error": "User not verified"},
            401,
            {"WWW-Authenticate": 'Basic realm = "Login required"'},
        )

# @app.route("/seturl", methods=["POST"])
# async def set_url():
#     auth = request.authorization
#     db_primary = await _get_db_primary()
#     prevuser = await db_primary.fetch_one(
#         "SELECT username FROM callbackurls where username = :username",
#         values={"username":auth.username},
#     )
#     if prevuser is not None:
#         return "", 409
#
#     return "", 200
#
#
@app.route("/test", methods=["POST"])
async def testing():
    envar = os.environ
    print(envar['HOSTNAME'])
    webhook_url = 'http://127.0.0.1:5500/payload'
    data = {'guesses':1}
    r = httpx.post(webhook_url,data=json.dumps(data), headers={'Content-Type': 'application/json'})
    return "",204

@app.route("/payload", methods=["POST"])
async def check_gamescore():
    print("HELLO")
    push = await request.get_json()
    app.logger.debug(json.dumps(push, indent=1))
    print("GOODBYE")
    print(push)
    return "",204

@app.route("/subscribe", methods=["POST"])
@validate_request(Url)
async def register(data):
    print("Whats up gamers")
    # auth = request.authorization
    db = await _get_db_primary()
    url = dataclasses.asdict(data)
    print(url.get('url'))
    # username = auth.username
    await db.execute("INSERT INTO callbackurls(username, url) VALUES(:username, :url)", values={"username":auth.username,"url":url.get('url')})
    checkdb = await db.fetch_one(
        "SELECT username FROM callbackurls where username = :username",
        values={"username":username},
    )
    envar = os.environ
    print(envar['HOSTNAME'])
    fqdn = socket.getfqdn(envar['HOSTNAME'])
    print(fqdn)
    leaderboardURL = 'http://'+fqdn+':5400/results'
    print(leaderboardURL)

    return "",200

@app.route("/connleaderboard", methods=["POST"])
async def connlb():
    db = await _get_db_primary()
    leaderboardURL = 'http://127.0.0.1:5400/results'
    username = "Franklin"
    await db.execute("INSERT INTO callbackurls(username, url) VALUES(:username, :url)", values={"username":username,"url":leaderboardURL})
    checkdb = await db.fetch_one(
        "SELECT username FROM callbackurls where username = :username",
        values={"username":username},
        )
    print(username)

    return "",200





@app.errorhandler(409)
def conflict(e):
    return {"error": str(e)}, 409
